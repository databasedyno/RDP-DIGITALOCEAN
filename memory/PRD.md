# Windows RDP Reseller — Product Requirements (PRD)

## Original problem statement
"What do I need to develop a way to resell Windows remote desktop?" → Build a backend API for a
Windows Remote Desktop reseller business wired to DigitalOcean for real server provisioning.

## Architecture
- **Backend:** FastAPI (`/app/backend`), all routes under `/api`.
  - `server.py` — routes (catalog, tiers, OS, pricing, orders, servers, provision callback, expiry admin)
  - `models.py` — pydantic models + request bodies
  - `do_client.py` — async DigitalOcean v2 API client (token server-side only)
  - `database.py` — Mongo connection + idempotent catalog/pricing seeding
  - `provisioning.py` — async provisioning orchestrator, password/token gen, expiry sweep
  - `scripts/convert_to_windows.sh` — Ubuntu→Windows conversion (cloud-init user-data)
  - `scripts/autounattend.xml.tmpl` — unattended Windows Server answer file
- **DB:** MongoDB — collections: `os_options`, `tiers`, `pricing`, `orders`, `servers`.
- **Provider:** DigitalOcean (token in `backend/.env` as `DIGITALOCEAN_TOKEN`). `PUBLIC_BASE_URL`
  is used as the conversion-script callback base.
- **Frontend:** none this phase (API-only).

## User personas
- **Reseller/admin (you):** configures tiers/pricing/OS ISO sources, marks orders paid, manages
  server lifecycle.
- **Customer (future phase):** picks OS + tier + duration + region, receives RDP credentials.

## Core requirements (static)
- OS offered: Windows Server 2019 / 2022 / 2025 only.
- Durations: 1 / 2 / 3 months.
- 4 default resource tiers (Starter/Standard/Pro/Power), each → a DO droplet size.
- Pricing configurable per tier × duration.
- Single-tenant: one droplet per order.
- Regions: live from DO (available only), enriched with country.
- No auth, no online payments this phase (orders marked paid manually).
- Provisioning is asynchronous (~20–45 min) via Ubuntu→Windows conversion; progress via callbacks.

## Implemented (2026-09-17)
- Catalog: `GET /api/catalog/{os,tiers,durations,regions,do-sizes}` — verified (live DO regions/sizes).
- Tier config: `POST/PUT/DELETE /api/tiers` (+ auto-seed pricing).
- OS config: `PUT /api/os/{id}` (ISO/virtio/image_name/supported).
- Pricing: `GET /api/pricing`, `PUT /api/pricing` — verified.
- Orders: `POST /api/orders`, `GET /api/orders`, `GET /api/orders/{id}`,
  `POST /api/orders/{id}/mark-paid` (creates real droplet + kicks off conversion) — verified end-to-end
  (droplet created, booted, public IP assigned, status → converting).
- Provisioning callback: `POST /api/provision/callback` (token-gated; drives converting→active + expiry).
- Servers: `GET /api/servers`, `GET /api/servers/{id}`, `GET /api/servers/{id}/status` (progress+logs+creds),
  lifecycle `reboot` (verified live), `power-off`, `power-on`, `rebuild`, `reset-password`, `destroy` (verified live).
- Expiry: `POST /api/admin/process-expiries` (suspends/powers off expired servers); `expires_at` set on activation.
- Conversion script + unattended answer file delivered; RDP enabled, unique Administrator password per order.

## Verified vs. not
- **Verified:** all catalog/pricing/order/tier endpoints, live DO connectivity, real droplet
  create → boot → IP → converting, reboot action, destroy action.
- **NOT fully verified:** the on-droplet Windows conversion running to completion (20–45 min) and a
  successful RDP login — this depends on the droplet-side conversion (KVM availability, ISO download,
  QEMU disk write) and was not waited out during testing. A live test server was left running for the
  user to validate RDP.

## Backlog / next phases
- P0: confirm end-to-end conversion → RDP login on the live test server; switch eval ISOs to SPLA media before selling.
- P1: auth (admin + customer), online payments + self-service checkout, automatic renewals/billing.
- P1: admin dashboard + customer storefront UI.
- P2: prebuilt DO custom Windows images (faster than per-order conversion), multi-user RDS option.
