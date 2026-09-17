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
    answer_b64 = base64.b64encode(answer.encode("utf-8")).decode("ascii")
    repl = {
        "{{ISO_URL}}": os_option["iso_url"],
        "{{VIRTIO_URL}}": os_option["virtio_url"],
        "{{CALLBACK_URL}}": f"{callback_base}/api/provision/callback",
        "{{CALLBACK_TOKEN}}": server["callback_token"],
        "{{SERVER_ID}}": server["id"],
        "{{ADMIN_PASSWORD}}": server["admin_password"],
        "{{AUTOUNATTEND_B64}}": answer_b64,
    }
    for k, v in repl.items():
        script = script.replace(k, v)
    return script


async def provision_server(server_id: str):
    """Create the Ubuntu droplet and poll until it boots. The droplet then runs
    the conversion script which reports Windows-install progress via callbacks."""
    try:
        server = await db.servers.find_one({"id": server_id}, {"_id": 0})
        if not server:
            return
        os_option = await db.os_options.find_one({"id": server["os_id"]}, {"_id": 0})
        callback_base = os.environ["PUBLIC_BASE_URL"].rstrip("/")

        await add_log(server_id, "creating", "Creating DigitalOcean droplet...", 5, "creating")
        user_data = build_user_data(server, os_option, callback_base)
        name = f"rdp-{server_id[:8]}"
        data = await do.create_droplet(
            name=name,
            region=server["region"],
            size=server["do_size_slug"],
            image=os.environ.get("DO_UBUNTU_IMAGE", "ubuntu-22-04-x64"),
            user_data=user_data,
            tags=["rdp-reseller"],
        )
        droplet = data.get("droplet", {})
        droplet_id = droplet.get("id")
        await db.servers.update_one({"id": server_id}, {"$set": {"do_droplet_id": droplet_id}})
        await add_log(server_id, "booting", f"Droplet {droplet_id} created. Waiting for boot...", 10, "booting")

        ip = None
        for _ in range(72):  # ~6 min
            await asyncio.sleep(5)
            d = (await do.get_droplet(droplet_id)).get("droplet", {})
            if d.get("status") == "active":
                for net in d.get("networks", {}).get("v4", []):
                    if net.get("type") == "public":
                        ip = net.get("ip_address")
                if ip:
                    break
        if ip:
            await db.servers.update_one({"id": server_id}, {"$set": {"ip_address": ip}})
            await add_log(server_id, "converting",
                          f"Ubuntu booted at {ip}. Windows conversion running on host (20-45 min).",
                          15, "converting")
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
