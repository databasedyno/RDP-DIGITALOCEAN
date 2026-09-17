import uuid
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field

# ---- helpers ----
DURATIONS = [1, 2, 3]  # months offered


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


# ---- stored documents ----
class OSOption(BaseModel):
    id: str
    name: str
    edition: str
    image_name: str          # Windows setup /IMAGE/NAME value
    iso_url: str             # configurable install ISO source
    virtio_url: str          # VirtIO driver ISO source
    supported: bool = True
    note: str = ""


class Tier(BaseModel):
    id: str
    slug: str
    name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    do_size_slug: str
    monthly_do_cost: float
    active: bool = True


class PricingEntry(BaseModel):
    tier_id: str
    duration_months: int
    sell_price: float


class LogEntry(BaseModel):
    ts: datetime = Field(default_factory=now_utc)
    stage: str
    message: str


class Order(BaseModel):
    id: str = Field(default_factory=new_id)
    os_id: str
    tier_id: str
    duration_months: int
    region: str
    customer_email: Optional[str] = None
    customer_name: Optional[str] = None
    price: float = 0
    status: str = "pending"   # pending, paid, provisioning, active, failed, cancelled
    server_id: Optional[str] = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class Server(BaseModel):
    id: str = Field(default_factory=new_id)
    order_id: str
    os_id: str
    tier_id: str
    duration_months: int
    region: str
    do_size_slug: str
    do_droplet_id: Optional[int] = None
    ip_address: Optional[str] = None
    admin_username: str = "Administrator"
    admin_password: str
    status: str = "queued"    # queued, creating, booting, converting, active, failed, suspended, expired, destroyed
    progress: int = 0
    callback_token: str
    logs: List[LogEntry] = []
    created_at: datetime = Field(default_factory=now_utc)
    activated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


# ---- request bodies ----
class CreateOrderReq(BaseModel):
    os_id: str
    tier_id: str
    duration_months: int
    region: str
    customer_email: Optional[str] = None
    customer_name: Optional[str] = None


class CreateTierReq(BaseModel):
    slug: str
    name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    do_size_slug: str
    monthly_do_cost: float
    active: bool = True


class UpdateTierReq(BaseModel):
    name: Optional[str] = None
    vcpu: Optional[int] = None
    ram_gb: Optional[int] = None
    disk_gb: Optional[int] = None
    do_size_slug: Optional[str] = None
    monthly_do_cost: Optional[float] = None
    active: Optional[bool] = None


class UpdateOSReq(BaseModel):
    name: Optional[str] = None
    iso_url: Optional[str] = None
    virtio_url: Optional[str] = None
    image_name: Optional[str] = None
    supported: Optional[bool] = None
    note: Optional[str] = None


class UpdatePricingReq(BaseModel):
    tier_id: str
    duration_months: int
    sell_price: float


class ProvisionCallbackReq(BaseModel):
    server_id: str
    token: str
    stage: str
    message: str = ""
    progress: Optional[int] = None
    status: Optional[str] = None
