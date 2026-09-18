from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import logging
from datetime import timezone
from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

import do_client as do
from do_client import DOError
from database import db, seed_catalog
from models import (
    DURATIONS, now_utc, new_id,
    CreateOrderReq, CreateTierReq, UpdateTierReq, UpdateOSReq,
    UpdatePricingReq, ProvisionCallbackReq, BuildImageReq,
)
from provisioning import (
    gen_password, gen_token, add_log, start_provisioning, start_reprovision,
    apply_activation, process_expiries, start_build_golden, SCRIPTS_DIR,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Windows RDP Reseller API")
api = APIRouter(prefix="/api")

REGION_COUNTRY = {
    "nyc": "United States", "sfo": "United States", "tor": "Canada",
    "lon": "United Kingdom", "ams": "Netherlands", "fra": "Germany",
    "blr": "India", "sgp": "Singapore", "syd": "Australia",
}


def region_country(slug: str) -> str:
    return REGION_COUNTRY.get(slug[:3], "Other")


async def get_price(tier_id: str, duration: int):
    doc = await db.pricing.find_one({"tier_id": tier_id, "duration_months": duration}, {"_id": 0})
    return doc["sell_price"] if doc else None


def is_expired(server: dict) -> bool:
    exp = server.get("expires_at")
    if not exp:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= now_utc()


# ---------------- meta ----------------
@api.get("/")
async def root():
    return {"service": "Windows RDP Reseller API", "status": "ok"}


# ---------------- catalog ----------------
@api.get("/catalog/os")
async def catalog_os():
    return await db.os_options.find({}, {"_id": 0}).to_list(100)


@api.get("/catalog/tiers")
async def catalog_tiers():
    return await db.tiers.find({}, {"_id": 0}).sort("monthly_do_cost", 1).to_list(100)


@api.get("/catalog/durations")
async def catalog_durations():
    return [{"months": d, "label": f"{d} month" + ("s" if d > 1 else "")} for d in DURATIONS]


@api.get("/catalog/regions")
async def catalog_regions():
    try:
        data = await do.list_regions()
    except DOError as e:
        raise HTTPException(e.status, e.message)
    out = []
    for r in data.get("regions", []):
        if not r.get("available"):
            continue
        out.append({
            "slug": r["slug"],
            "name": r["name"],
            "country": region_country(r["slug"]),
            "sizes": r.get("sizes", []),
        })
    return out


@api.get("/catalog/do-sizes")
async def catalog_do_sizes():
    """Live DigitalOcean size slugs, to help map resource tiers."""
    try:
        data = await do.list_sizes()
    except DOError as e:
        raise HTTPException(e.status, e.message)
    return [{
        "slug": s["slug"], "vcpus": s["vcpus"], "memory_mb": s["memory"],
        "disk_gb": s["disk"], "price_monthly": s["price_monthly"],
        "regions": s.get("regions", []),
    } for s in data.get("sizes", []) if s.get("available")]


# ---------------- tiers config ----------------
@api.post("/tiers")
async def create_tier(req: CreateTierReq):
    if await db.tiers.find_one({"id": req.slug}):
        raise HTTPException(400, "Tier slug already exists")
    tier = {"id": req.slug, **req.model_dump()}
    await db.tiers.insert_one(dict(tier))
    for dur in DURATIONS:
        await db.pricing.insert_one({
            "tier_id": req.slug, "duration_months": dur,
            "sell_price": round(req.monthly_do_cost * dur * 2, 2),
        })
    tier.pop("_id", None)
    return tier


@api.put("/tiers/{tier_id}")
async def update_tier(tier_id: str, req: UpdateTierReq):
    changes = {k: v for k, v in req.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "No fields to update")
    res = await db.tiers.update_one({"id": tier_id}, {"$set": changes})
    if res.matched_count == 0:
        raise HTTPException(404, "Tier not found")
    return await db.tiers.find_one({"id": tier_id}, {"_id": 0})


@api.delete("/tiers/{tier_id}")
async def delete_tier(tier_id: str):
    res = await db.tiers.delete_one({"id": tier_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Tier not found")
    await db.pricing.delete_many({"tier_id": tier_id})
    return {"deleted": tier_id}


# ---------------- OS config ----------------
@api.put("/os/{os_id}")
async def update_os(os_id: str, req: UpdateOSReq):
    changes = {k: v for k, v in req.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "No fields to update")
    res = await db.os_options.update_one({"id": os_id}, {"$set": changes})
    if res.matched_count == 0:
        raise HTTPException(404, "OS option not found")
    return await db.os_options.find_one({"id": os_id}, {"_id": 0})


# ---------------- golden images (Phase 6) ----------------
@api.get("/provision/bootscript", response_class=PlainTextResponse)
async def bootscript():
    return (SCRIPTS_DIR / "apply.ps1").read_text()


@api.post("/os/{os_id}/build-image")
async def build_image(os_id: str, req: BuildImageReq):
    os_opt = await db.os_options.find_one({"id": os_id}, {"_id": 0})
    if not os_opt:
        raise HTTPException(404, "OS option not found")
    if os_opt.get("golden_status") == "building":
        raise HTTPException(400, "A golden image build is already in progress for this OS")
    start_build_golden(os_id, req.region)
    return {"os_id": os_id, "region": req.region, "status": "building"}


@api.get("/os/{os_id}/build-status")
async def build_status(os_id: str):
    build = await db.image_builds.find_one({"os_id": os_id}, {"_id": 0}, sort=[("created_at", -1)])
    if not build:
        raise HTTPException(404, "No build found for this OS")
    return build


@api.get("/images")
async def list_images():
    out = []
    for o in await db.os_options.find({}, {"_id": 0}).to_list(100):
        out.append({
            "os_id": o["id"], "name": o["name"], "golden_status": o.get("golden_status", "none"),
            "golden_image_id": o.get("golden_image_id"), "golden_regions": o.get("golden_regions", []),
            "golden_min_disk_gb": o.get("golden_min_disk_gb", 0),
        })
    return out


# ---------------- pricing ----------------
@api.get("/pricing")
async def list_pricing():
    tiers = {t["id"]: t for t in await db.tiers.find({}, {"_id": 0}).to_list(100)}
    entries = await db.pricing.find({}, {"_id": 0}).to_list(1000)
    out = []
    for e in entries:
        t = tiers.get(e["tier_id"], {})
        out.append({
            **e,
            "tier_name": t.get("name", e["tier_id"]),
            "do_cost": round(t.get("monthly_do_cost", 0) * e["duration_months"], 2),
        })
    out.sort(key=lambda x: (x["tier_name"], x["duration_months"]))
    return out


@api.put("/pricing")
async def update_pricing(req: UpdatePricingReq):
    if req.duration_months not in DURATIONS:
        raise HTTPException(400, "Invalid duration")
    if not await db.tiers.find_one({"id": req.tier_id}):
        raise HTTPException(404, "Tier not found")
    await db.pricing.update_one(
        {"tier_id": req.tier_id, "duration_months": req.duration_months},
        {"$set": {"sell_price": req.sell_price}}, upsert=True,
    )
    return {"tier_id": req.tier_id, "duration_months": req.duration_months,
            "sell_price": req.sell_price}


# ---------------- orders ----------------
@api.post("/orders")
async def create_order(req: CreateOrderReq):
    os_opt = await db.os_options.find_one({"id": req.os_id}, {"_id": 0})
    if not os_opt or not os_opt.get("supported"):
        raise HTTPException(400, "Invalid or unsupported OS")
    tier = await db.tiers.find_one({"id": req.tier_id, "active": True}, {"_id": 0})
    if not tier:
        raise HTTPException(400, "Invalid or inactive tier")
    if req.duration_months not in DURATIONS:
        raise HTTPException(400, "Invalid duration")
    price = await get_price(req.tier_id, req.duration_months)
    if price is None:
        raise HTTPException(400, "No price configured for this tier/duration")

    order = {
        "id": new_id(), "os_id": req.os_id, "tier_id": req.tier_id,
        "duration_months": req.duration_months, "region": req.region,
        "customer_email": req.customer_email, "customer_name": req.customer_name,
        "price": price, "status": "pending", "server_id": None,
        "created_at": now_utc(), "updated_at": now_utc(),
    }
    await db.orders.insert_one(dict(order))
    order.pop("_id", None)
    return order


@api.get("/orders")
async def list_orders():
    return await db.orders.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)


@api.get("/orders/{order_id}")
async def get_order(order_id: str):
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
    return order


@api.post("/orders/{order_id}/mark-paid")
async def mark_paid(order_id: str):
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
    if order["status"] not in ("pending",):
        raise HTTPException(400, f"Order is already '{order['status']}'")
    tier = await db.tiers.find_one({"id": order["tier_id"]}, {"_id": 0})

    server = {
        "id": new_id(), "order_id": order_id, "os_id": order["os_id"],
        "tier_id": order["tier_id"], "duration_months": order["duration_months"],
        "region": order["region"], "do_size_slug": tier["do_size_slug"],
        "do_droplet_id": None, "ip_address": None, "admin_username": "Administrator",
        "admin_password": gen_password(), "status": "queued", "progress": 0,
        "callback_token": gen_token(), "logs": [], "created_at": now_utc(),
        "activated_at": None, "expires_at": None,
    }
    await db.servers.insert_one(dict(server))
    await db.orders.update_one(
        {"id": order_id},
        {"$set": {"status": "provisioning", "server_id": server["id"], "updated_at": now_utc()}},
    )
    await add_log(server["id"], "queued", "Order marked paid. Provisioning queued.", 0)
    start_provisioning(server["id"])
    server.pop("_id", None)
    return {"order_id": order_id, "server_id": server["id"], "status": "provisioning"}


# ---------------- provisioning callback (called by the droplet) ----------------
@api.post("/provision/callback")
async def provision_callback(req: ProvisionCallbackReq):
    server = await db.servers.find_one({"id": req.server_id}, {"_id": 0})
    if not server or server.get("callback_token") != req.token:
        raise HTTPException(403, "Invalid callback token")
    status = req.status
    if req.stage == "rdp_ready" or (req.progress is not None and req.progress >= 100):
        await apply_activation(server)
    elif req.stage == "failed":
        await add_log(req.server_id, "failed", req.message or "Conversion failed.", req.progress, "failed")
        await db.orders.update_one({"server_id": req.server_id}, {"$set": {"status": "failed"}})
    else:
        status = status or "converting"
        await add_log(req.server_id, req.stage, req.message or req.stage, req.progress, status)
    return {"ok": True}


# ---------------- servers ----------------
def _server_view(s: dict) -> dict:
    s = dict(s)
    s["is_expired"] = is_expired(s)
    return s


@api.get("/servers")
async def list_servers():
    servers = await db.servers.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return [_server_view(s) for s in servers]


@api.get("/servers/{server_id}")
async def get_server(server_id: str):
    s = await db.servers.find_one({"id": server_id}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Server not found")
    return _server_view(s)


@api.get("/servers/{server_id}/status")
async def server_status(server_id: str):
    s = await db.servers.find_one({"id": server_id}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Server not found")
    return {
        "id": s["id"], "status": s["status"], "progress": s["progress"],
        "ip_address": s.get("ip_address"), "is_expired": is_expired(s),
        "expires_at": s.get("expires_at"), "logs": s.get("logs", []),
        "credentials": {
            "ip_address": s.get("ip_address"),
            "username": s.get("admin_username"),
            "password": s.get("admin_password"),
        } if s["status"] == "active" else None,
    }


async def _require_server(server_id: str):
    s = await db.servers.find_one({"id": server_id}, {"_id": 0})
    if not s:
        raise HTTPException(404, "Server not found")
    return s


async def _do_action(s: dict, body: dict):
    if not s.get("do_droplet_id"):
        raise HTTPException(400, "Server has no droplet yet")
    try:
        return await do.droplet_action(s["do_droplet_id"], body)
    except DOError as e:
        raise HTTPException(e.status, e.message)


@api.post("/servers/{server_id}/reboot")
async def reboot_server(server_id: str):
    s = await _require_server(server_id)
    res = await _do_action(s, {"type": "reboot"})
    await add_log(server_id, "action", "Reboot requested.")
    return {"ok": True, "action": res.get("action")}


@api.post("/servers/{server_id}/power-off")
async def power_off_server(server_id: str):
    s = await _require_server(server_id)
    res = await _do_action(s, {"type": "power_off"})
    await db.servers.update_one({"id": server_id}, {"$set": {"status": "suspended"}})
    await add_log(server_id, "action", "Power off / suspend requested.")
    return {"ok": True, "action": res.get("action")}


@api.post("/servers/{server_id}/power-on")
async def power_on_server(server_id: str):
    s = await _require_server(server_id)
    res = await _do_action(s, {"type": "power_on"})
    if not is_expired(s):
        await db.servers.update_one({"id": server_id}, {"$set": {"status": "active"}})
    await add_log(server_id, "action", "Power on requested.")
    return {"ok": True, "action": res.get("action")}


@api.post("/servers/{server_id}/rebuild")
async def rebuild_server(server_id: str):
    await _require_server(server_id)
    await add_log(server_id, "action", "Rebuild requested. Re-running Windows conversion.")
    start_reprovision(server_id)
    return {"ok": True, "status": "queued"}


@api.post("/servers/{server_id}/reset-password")
async def reset_password(server_id: str):
    await _require_server(server_id)
    new_pw = gen_password()
    await db.servers.update_one({"id": server_id}, {"$set": {"admin_password": new_pw}})
    await add_log(server_id, "action", "Password reset requested. Re-provisioning with new password.")
    start_reprovision(server_id)
    return {"ok": True, "new_password": new_pw, "status": "queued"}


@api.post("/servers/{server_id}/destroy")
async def destroy_server(server_id: str):
    s = await _require_server(server_id)
    if s.get("do_droplet_id"):
        try:
            await do.delete_droplet(s["do_droplet_id"])
        except DOError as e:
            if e.status != 404:
                raise HTTPException(e.status, e.message)
    await db.servers.update_one(
        {"id": server_id},
        {"$set": {"status": "destroyed", "ip_address": None, "do_droplet_id": None}},
    )
    await db.orders.update_one({"id": s["order_id"]}, {"$set": {"status": "cancelled"}})
    await add_log(server_id, "action", "Server destroyed.")
    return {"ok": True, "status": "destroyed"}


# ---------------- expiry admin ----------------
@api.post("/admin/process-expiries")
async def admin_process_expiries():
    suspended = await process_expiries()
    return {"expired_suspended": suspended, "count": len(suspended)}


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    await seed_catalog()
    logger.info("Catalog seeded.")


@app.on_event("shutdown")
async def _shutdown():
    db.client.close() if hasattr(db, "client") else None
