import asyncio
import os
import secrets
import base64
from pathlib import Path
from datetime import timedelta

from database import db
from models import now_utc
import do_client as do

SCRIPTS_DIR = Path(__file__).parent / "scripts"
_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%^*()-_=+"

# Golden-image build droplet: needs >=4 GB RAM for the QEMU install. Its disk
# (80 GB) becomes the minimum disk for droplets launched from the snapshot.
BUILD_SIZE = "s-2vcpu-4gb"
BUILD_DISK_GB = 80


def gen_password(n: int = 18) -> str:
    # Avoid $ ` " ' \ & < > so it is safe inside shell double-quotes, XML and JSON.
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(n))


def gen_token() -> str:
    return secrets.token_urlsafe(24)


async def add_log(server_id, stage, message, progress=None, status=None):
    entry = {"ts": now_utc(), "stage": stage, "message": message}
    setter = {"updated_at": now_utc()}
    if progress is not None:
        setter["progress"] = progress
    if status is not None:
        setter["status"] = status
    await db.servers.update_one(
        {"id": server_id},
        {"$push": {"logs": entry}, "$set": setter},
    )


def build_user_data(server: dict, os_option: dict, callback_base: str) -> str:
    script = (SCRIPTS_DIR / "convert_to_windows.sh").read_text()
    answer = (SCRIPTS_DIR / "autounattend.xml.tmpl").read_text()
    answer = answer.replace("{{ADMIN_PASSWORD}}", server["admin_password"])
    answer = answer.replace("{{IMAGE_NAME}}", os_option["image_name"])
    answer = answer.replace("{{BOOTSCRIPT_URL}}", f"{callback_base}/api/provision/bootscript")
    answer_b64 = base64.b64encode(answer.encode("utf-8")).decode("ascii")
    repl = {
        "{{ISO_URL}}": os_option["iso_url"],
        "{{VIRTIO_URL}}": os_option["virtio_url"],
        "{{INSTALL_METHOD}}": os_option.get("install_method", "qemu"),
        "{{IMAGE_URL}}": os_option.get("image_url", ""),
        "{{VIRTIO_DIR}}": os_option.get("virtio_dir", "2k22"),
        "{{CALLBACK_URL}}": f"{callback_base}/api/provision/callback",
        "{{CALLBACK_TOKEN}}": server["callback_token"],
        "{{SERVER_ID}}": server["id"],
        "{{ADMIN_PASSWORD}}": server["admin_password"],
        "{{AUTOUNATTEND_B64}}": answer_b64,
    }
    for k, v in repl.items():
        script = script.replace(k, v)
    return script


def build_metadata_userdata(server: dict, callback_base: str) -> str:
    """Plain KEY=VALUE user-data read by the baked apply.ps1 on golden-image droplets."""
    return (
        f"ADMIN_PASSWORD={server['admin_password']}\n"
        f"CALLBACK_URL={callback_base}/api/provision/callback\n"
        f"CALLBACK_TOKEN={server['callback_token']}\n"
        f"SERVER_ID={server['id']}\n"
    )


async def _tcp_open(ip: str, port: int = 3389, timeout: int = 5) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def poll_rdp(server_id: str, ip: str, minutes: int = 90):
    """Mark a server active only when RDP (3389) actually becomes reachable."""
    waited, interval, deadline = 0, 30, minutes * 60
    while waited < deadline:
        await asyncio.sleep(interval)
        waited += interval
        s = await db.servers.find_one({"id": server_id}, {"_id": 0, "status": 1})
        if not s or s["status"] in ("destroyed", "failed", "active", "suspended", "expired"):
            return
        if await _tcp_open(ip, 3389):
            srv = await db.servers.find_one({"id": server_id}, {"_id": 0})
            await apply_activation(srv)
            await add_log(server_id, "rdp_ready", f"RDP reachable at {ip}:3389. Server is active.", 100, "active")
            return
    await add_log(server_id, "failed",
                  f"RDP not reachable at {ip}:3389 after {minutes} min. Windows conversion did not complete.",
                  None, "failed")
    await db.orders.update_one({"server_id": server_id}, {"$set": {"status": "failed"}})


async def _wait_boot_ip(droplet_id: int, tries: int = 72):
    ip = None
    for _ in range(tries):
        await asyncio.sleep(5)
        d = (await do.get_droplet(droplet_id)).get("droplet", {})
        if d.get("status") == "active":
            for net in d.get("networks", {}).get("v4", []):
                if net.get("type") == "public":
                    ip = net.get("ip_address")
            if ip:
                break
    return ip


async def provision_server(server_id: str):
    """Provision an order. Uses the fast golden-image path when available for the
    OS + region + tier; otherwise falls back to the full Windows conversion."""
    try:
        server = await db.servers.find_one({"id": server_id}, {"_id": 0})
        if not server:
            return
        os_option = await db.os_options.find_one({"id": server["os_id"]}, {"_id": 0})
        tier = await db.tiers.find_one({"id": server["tier_id"]}, {"_id": 0}) or {}
        callback_base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
        name = f"rdp-{server_id[:8]}"

        golden_ready = (os_option.get("golden_status") == "available"
                        and os_option.get("golden_image_id"))
        disk_ok = tier.get("disk_gb", 0) >= os_option.get("golden_min_disk_gb", 10**9)
        region_ready = server["region"] in os_option.get("golden_regions", [])

        # ---- fast path: create straight from the golden snapshot ----
        if golden_ready and disk_ok and region_ready:
            await add_log(server_id, "creating",
                          "Creating droplet from golden image (fast, ~2-3 min)...", 10, "creating")
            data = await do.create_droplet(
                name=name, region=server["region"], size=server["do_size_slug"],
                image=os_option["golden_image_id"],
                user_data=build_metadata_userdata(server, callback_base),
                tags=["rdp-reseller"],
            )
            droplet_id = data.get("droplet", {}).get("id")
            await db.servers.update_one({"id": server_id}, {"$set": {"do_droplet_id": droplet_id}})
            await add_log(server_id, "booting", f"Droplet {droplet_id} created from image. Booting...", 30, "booting")
            ip = await _wait_boot_ip(droplet_id)
            if ip:
                await db.servers.update_one({"id": server_id}, {"$set": {"ip_address": ip}})
                await add_log(server_id, "installing",
                              f"Booted from image at {ip}. Applying config + password.", 60, "installing")
                asyncio.create_task(poll_rdp(server_id, ip, minutes=20))
            else:
                await add_log(server_id, "installing", "Droplet active; awaiting first boot.", 60, "installing")
            return

        # If a golden image exists but not yet in this region, transfer it for next time.
        if golden_ready and disk_ok and not region_ready:
            asyncio.create_task(transfer_image(os_option["golden_image_id"], server["region"], os_option["id"]))
            await add_log(server_id, "info",
                          "Golden image not in this region yet - using full conversion this time "
                          "(image transfer started for future orders).", 5)

        # ---- fallback: full Ubuntu -> Windows conversion ----
        await add_log(server_id, "creating", "Creating DigitalOcean droplet...", 5, "creating")
        user_data = build_user_data(server, os_option, callback_base)
        data = await do.create_droplet(
            name=name, region=server["region"], size=server["do_size_slug"],
            image=os.environ.get("DO_UBUNTU_IMAGE", "ubuntu-22-04-x64"),
            user_data=user_data, tags=["rdp-reseller"],
        )
        droplet_id = data.get("droplet", {}).get("id")
        await db.servers.update_one({"id": server_id}, {"$set": {"do_droplet_id": droplet_id}})
        await add_log(server_id, "booting", f"Droplet {droplet_id} created. Waiting for boot...", 10, "booting")
        ip = await _wait_boot_ip(droplet_id)
        if ip:
            await db.servers.update_one({"id": server_id}, {"$set": {"ip_address": ip}})
            await add_log(server_id, "converting",
                          f"Ubuntu booted at {ip}. Windows conversion running on host (20-45 min).",
                          15, "converting")
            asyncio.create_task(poll_rdp(server_id, ip))
        else:
            await add_log(server_id, "converting",
                          "Droplet active; awaiting Windows conversion callbacks.", 15, "converting")
    except Exception as e:  # noqa: BLE001
        await add_log(server_id, "failed", f"Provisioning error: {e}", None, "failed")
        await db.orders.update_one({"server_id": server_id}, {"$set": {"status": "failed"}})


async def reprovision_server(server_id: str):
    server = await db.servers.find_one({"id": server_id}, {"_id": 0})
    if not server:
        return
    if server.get("do_droplet_id"):
        try:
            await do.delete_droplet(server["do_droplet_id"])
        except Exception:  # noqa: BLE001
            pass
    await db.servers.update_one(
        {"id": server_id},
        {"$set": {"do_droplet_id": None, "ip_address": None, "progress": 0,
                  "status": "queued", "logs": [], "activated_at": None, "expires_at": None}},
    )
    await provision_server(server_id)


def start_provisioning(server_id: str):
    asyncio.create_task(provision_server(server_id))


def start_reprovision(server_id: str):
    asyncio.create_task(reprovision_server(server_id))


async def apply_activation(server: dict):
    """Called from the callback when Windows is live: set expiry from plan duration."""
    activated = now_utc()
    expires = activated + timedelta(days=30 * server["duration_months"])
    await db.servers.update_one(
        {"id": server["id"]},
        {"$set": {"status": "active", "progress": 100,
                  "activated_at": activated, "expires_at": expires}},
    )
    await db.orders.update_one({"id": server["order_id"]}, {"$set": {"status": "active"}})


async def process_expiries():
    """Suspend (power off) servers whose plan has expired."""
    now = now_utc()
    suspended = []
    cursor = db.servers.find({"status": "active", "expires_at": {"$lte": now}}, {"_id": 0})
    async for s in cursor:
        try:
            if s.get("do_droplet_id"):
                await do.droplet_action(s["do_droplet_id"], {"type": "power_off"})
        except Exception:  # noqa: BLE001
            pass
        await db.servers.update_one({"id": s["id"]}, {"$set": {"status": "expired"}})
        suspended.append(s["id"])
    return suspended


# ==================== Phase 6: golden image builds ====================
async def add_build_log(build_id, stage, message, progress=None, status=None):
    entry = {"ts": now_utc(), "stage": stage, "message": message}
    setter = {"updated_at": now_utc()}
    if progress is not None:
        setter["progress"] = progress
    if status is not None:
        setter["status"] = status
    await db.image_builds.update_one(
        {"id": build_id}, {"$push": {"logs": entry}, "$set": setter}
    )


async def _wait_action(action_id: int, minutes: int = 30) -> bool:
    waited, deadline = 0, minutes * 60
    while waited < deadline:
        await asyncio.sleep(15)
        waited += 15
        a = (await do.get_action(action_id)).get("action", {})
        if a.get("status") == "completed":
            return True
        if a.get("status") == "errored":
            return False
    return False


async def _wait_droplet_off(droplet_id: int, minutes: int = 10) -> bool:
    waited, deadline = 0, minutes * 60
    while waited < deadline:
        await asyncio.sleep(10)
        waited += 10
        d = (await do.get_droplet(droplet_id)).get("droplet", {})
        if d.get("status") == "off":
            return True
    return False


async def transfer_image(image_id: int, region: str, os_id: str):
    """Transfer a snapshot to another region and register it as available there."""
    try:
        act = (await do.image_action(image_id, {"type": "transfer", "region": region})).get("action", {})
        if act.get("id") and await _wait_action(act["id"], minutes=30):
            await db.os_options.update_one({"id": os_id}, {"$addToSet": {"golden_regions": region}})
    except Exception:  # noqa: BLE001
        pass


async def build_golden_image(os_id: str, region: str = "nyc3"):
    build_id = new_build_id()
    build = {
        "id": build_id, "os_id": os_id, "region": region, "status": "building",
        "progress": 0, "logs": [], "callback_token": gen_token(),
        "do_droplet_id": None, "snapshot_image_id": None, "created_at": now_utc(),
    }
    await db.image_builds.insert_one(dict(build))
    await db.os_options.update_one(
        {"id": os_id}, {"$set": {"golden_status": "building", "golden_build_id": build_id}}
    )
    try:
        os_option = await db.os_options.find_one({"id": os_id}, {"_id": 0})
        callback_base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
        pseudo = {"id": build_id, "admin_password": gen_password(), "callback_token": build["callback_token"]}
        user_data = build_user_data(pseudo, os_option, callback_base)

        await add_build_log(build_id, "creating", f"Creating build droplet ({BUILD_SIZE}) in {region}...", 5)
        ssh_key = os.environ.get("DO_SSH_KEY_ID")
        data = await do.create_droplet(
            name=f"golden-{os_id}", region=region, size=BUILD_SIZE,
            image=os.environ.get("DO_UBUNTU_IMAGE", "ubuntu-22-04-x64"),
            user_data=user_data, tags=["golden-build"],
            ssh_keys=[int(ssh_key)] if ssh_key else None,
        )
        droplet_id = data.get("droplet", {}).get("id")
        await db.image_builds.update_one({"id": build_id}, {"$set": {"do_droplet_id": droplet_id}})
        await add_build_log(build_id, "booting", f"Droplet {droplet_id} created. Waiting for boot...", 10)

        ip = await _wait_boot_ip(droplet_id)
        if not ip:
            raise RuntimeError("build droplet never got a public IP")
        await add_build_log(build_id, "converting",
                            f"Ubuntu booted at {ip}. Converting to Windows (~30 min)...", 20)

        # Wait until the freshly-installed Windows is RDP-reachable.
        up = False
        waited = 0
        while waited < 60 * 60:
            await asyncio.sleep(30)
            waited += 30
            if await _tcp_open(ip, 3389):
                up = True
                break
        if not up:
            raise RuntimeError("Windows never became RDP-reachable within 60 min")
        await add_build_log(build_id, "snapshotting", "Windows is up. Powering off to snapshot...", 70)

        await do.droplet_action(droplet_id, {"type": "power_off"})
        await _wait_droplet_off(droplet_id)
        snap_name = f"golden-{os_id}-{int(now_utc().timestamp())}"
        act = (await do.droplet_action(droplet_id, {"type": "snapshot", "name": snap_name})).get("action", {})
        await add_build_log(build_id, "snapshotting", "Snapshot in progress (Windows images take a while)...", 80)
        if not (act.get("id") and await _wait_action(act["id"], minutes=45)):
            raise RuntimeError("snapshot action did not complete")

        snaps = (await do.droplet_snapshots(droplet_id)).get("snapshots", [])
        if not snaps:
            raise RuntimeError("no snapshot found after completion")
        image_id = snaps[-1]["id"]

        await db.os_options.update_one({"id": os_id}, {"$set": {
            "golden_image_id": image_id, "golden_region": region, "golden_regions": [region],
            "golden_min_disk_gb": BUILD_DISK_GB, "golden_status": "available",
        }})
        await db.image_builds.update_one({"id": build_id}, {"$set": {
            "snapshot_image_id": image_id, "status": "available", "progress": 100,
        }})
        try:
            await do.delete_droplet(droplet_id)
        except Exception:  # noqa: BLE001
            pass
        await add_build_log(build_id, "done",
                            f"Golden image ready (image id {image_id}) in {region}. Build droplet destroyed.",
                            100, "available")
    except Exception as e:  # noqa: BLE001
        await add_build_log(build_id, "failed", f"Build failed: {e}", None, "failed")
        await db.os_options.update_one({"id": os_id}, {"$set": {"golden_status": "failed"}})


def new_build_id() -> str:
    return "build-" + secrets.token_hex(6)


def start_build_golden(os_id: str, region: str = "nyc3"):
    asyncio.create_task(build_golden_image(os_id, region))
