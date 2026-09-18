"""Thin async DigitalOcean v2 API client. Token stays server-side only."""
import os
import httpx

DO_BASE = "https://api.digitalocean.com/v2"


class DOError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"DigitalOcean API error {status}: {message}")


def _headers():
    token = os.environ["DIGITALOCEAN_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def do_request(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(base_url=DO_BASE, headers=_headers(), timeout=60) as c:
        r = await c.request(method, path, **kwargs)
    if r.status_code >= 400:
        try:
            detail = r.json().get("message", r.text[:500])
        except Exception:
            detail = r.text[:500]
        raise DOError(r.status_code, detail)
    return r.json() if r.content else {}


async def list_regions():
    return await do_request("GET", "/regions", params={"per_page": 200})


async def list_sizes():
    return await do_request("GET", "/sizes", params={"per_page": 200})


async def create_droplet(name, region, size, image, user_data=None, tags=None, ssh_keys=None):
    body = {"name": name, "region": region, "size": size, "image": image,
            "tags": tags or []}
    if ssh_keys:
        body["ssh_keys"] = ssh_keys
    if user_data is not None:
        body["user_data"] = user_data
    return await do_request("POST", "/droplets", json=body)


async def get_droplet(droplet_id: int):
    return await do_request("GET", f"/droplets/{droplet_id}")


async def droplet_action(droplet_id: int, body: dict):
    return await do_request("POST", f"/droplets/{droplet_id}/actions", json=body)


async def delete_droplet(droplet_id: int):
    return await do_request("DELETE", f"/droplets/{droplet_id}")


async def get_action(action_id: int):
    return await do_request("GET", f"/actions/{action_id}")


async def droplet_snapshots(droplet_id: int):
    return await do_request("GET", f"/droplets/{droplet_id}/snapshots")


async def image_action(image_id: int, body: dict):
    return await do_request("POST", f"/images/{image_id}/actions", json=body)
