import os
from motor.motor_asyncio import AsyncIOMotorClient
from models import DURATIONS

client = AsyncIOMotorClient(os.environ["MONGO_URL"], tz_aware=True)
db = client[os.environ["DB_NAME"]]

# Microsoft evaluation ISOs (configurable per OS via API). VirtIO from Fedora.
_VIRTIO = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"

DEFAULT_OS = [
    {
        "id": "ws2019",
        "name": "Windows Server 2019",
        "edition": "Standard (Desktop Experience)",
        "image_name": "Windows Server 2019 SERVERSTANDARD",
        "virtio_dir": "2k19",
        "iso_url": "https://go.microsoft.com/fwlink/p/?linkid=2195167&clcid=0x409&culture=en-us&country=US",
        "virtio_url": _VIRTIO,
        "install_method": "qemu",
        "image_url": "",
        "supported": True,
        "note": "180-day evaluation. Switch to SPLA-licensed media before selling.",
    },
    {
        "id": "ws2022",
        "name": "Windows Server 2022",
        "edition": "Standard (Desktop Experience)",
        "image_name": "Windows Server 2022 SERVERSTANDARD",
        "virtio_dir": "2k22",
        "iso_url": "https://go.microsoft.com/fwlink/p/?LinkID=2195280&clcid=0x409&culture=en-us&country=US",
        "virtio_url": _VIRTIO,
        "install_method": "qemu",
        "image_url": "",
        "supported": True,
        "note": "180-day evaluation. Switch to SPLA-licensed media before selling.",
    },
    {
        "id": "ws2025",
        "name": "Windows Server 2025",
        "edition": "Standard (Desktop Experience)",
        "image_name": "Windows Server 2025 SERVERSTANDARD",
        "virtio_dir": "2k25",
        "iso_url": "https://go.microsoft.com/fwlink/?linkid=2293312&clcid=0x409&culture=en-us&country=US",
        "virtio_url": _VIRTIO,
        "install_method": "qemu",
        "image_url": "",
        "supported": True,
        "note": "180-day evaluation. Switch to SPLA-licensed media before selling.",
    },
]

# id == slug for deterministic seeding. monthly_do_cost is your DigitalOcean cost reference.
DEFAULT_TIERS = [
    {"id": "starter", "slug": "starter", "name": "Starter", "vcpu": 1, "ram_gb": 2,
     "disk_gb": 50, "do_size_slug": "s-1vcpu-2gb", "monthly_do_cost": 12.0, "active": True},
    {"id": "standard", "slug": "standard", "name": "Standard", "vcpu": 2, "ram_gb": 4,
     "disk_gb": 80, "do_size_slug": "s-2vcpu-4gb", "monthly_do_cost": 24.0, "active": True},
    {"id": "pro", "slug": "pro", "name": "Pro", "vcpu": 4, "ram_gb": 8,
     "disk_gb": 160, "do_size_slug": "s-4vcpu-8gb", "monthly_do_cost": 48.0, "active": True},
    {"id": "power", "slug": "power", "name": "Power", "vcpu": 8, "ram_gb": 16,
     "disk_gb": 320, "do_size_slug": "s-8vcpu-16gb", "monthly_do_cost": 96.0, "active": True},
]


async def seed_catalog():
    for o in DEFAULT_OS:
        await db.os_options.update_one({"id": o["id"]}, {"$setOnInsert": o}, upsert=True)
    # Ensure newer fields exist on previously-seeded docs.
    await db.os_options.update_many(
        {"install_method": {"$exists": False}},
        {"$set": {"install_method": "qemu", "image_url": ""}},
    )
    await db.os_options.update_many(
        {"golden_status": {"$exists": False}},
        {"$set": {"golden_image_id": None, "golden_region": None, "golden_regions": [],
                  "golden_min_disk_gb": 0, "golden_status": "none", "golden_build_id": None}},
    )
    await db.os_options.update_one({"id": "ws2019", "virtio_dir": {"$exists": False}}, {"$set": {"virtio_dir": "2k19"}})
    await db.os_options.update_one({"id": "ws2022", "virtio_dir": {"$exists": False}}, {"$set": {"virtio_dir": "2k22"}})
    await db.os_options.update_one({"id": "ws2025", "virtio_dir": {"$exists": False}}, {"$set": {"virtio_dir": "2k25"}})
    for t in DEFAULT_TIERS:
        await db.tiers.update_one({"id": t["id"]}, {"$setOnInsert": t}, upsert=True)
    # Seed placeholder sell prices (~2x DO cost) for each active tier x duration.
    tiers = await db.tiers.find({}, {"_id": 0}).to_list(100)
    for t in tiers:
        for dur in DURATIONS:
            exists = await db.pricing.find_one({"tier_id": t["id"], "duration_months": dur})
            if not exists:
                await db.pricing.insert_one({
                    "tier_id": t["id"],
                    "duration_months": dur,
                    "sell_price": round(t["monthly_do_cost"] * dur * 2, 2),
                })
