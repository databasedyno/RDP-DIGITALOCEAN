# Windows RDP Reseller — Backend API (Phase 1)

## What this builds
A backend API for a Windows Remote Desktop reseller business, wired to **DigitalOcean** for
real server provisioning. It manages the OS catalog, pricing plans, customer orders, and the
full lifecycle of provisioned RDP servers. No website UI in this phase — it is API-only and
tested via API calls.

## Confirmed decisions
- **OS options offered:** Windows Server 2019, Windows Server 2022, Windows Server 2025 only.
  (Windows 10/11 removed.)
- **Plan durations:** 1 month, 2 months, 3 months only.
- **Resource tiers:** a plan = a resource tier (RAM/vCPU/disk) × a duration. Each tier maps to a
  DigitalOcean droplet size (disk is bundled with the tier). Ships with 4 editable default tiers:
  - Starter — 1 vCPU / 2 GB / 50 GB (minimum viable for Windows Server; ~$12/mo DO cost)
  - Standard — 2 vCPU / 4 GB / 80 GB (recommended entry; ~$24/mo DO cost)
  - Pro — 4 vCPU / 8 GB / 160 GB (~$48/mo DO cost)
  - Power — 8 vCPU / 16 GB / 320 GB (~$96/mo DO cost)
  Windows Server needs ~20–30 GB just for the OS, so 2 GB/50 GB is the floor; 4 GB is the real
  entry point.
- **Pricing:** fully configurable per **tier × duration** (you set the sell numbers). Duration
  multiplies cost (e.g. Standard DO cost $24/$48/$72 for 1/2/3 months). Ships with placeholder
  demo sell prices (~2× cost) that you overwrite.
- **Provider:** DigitalOcean, using the API key you already provided (stored securely in
  backend config). No other provider, no other key.
- **Install method — automated Ubuntu→Windows conversion:** since DigitalOcean has no native
  Windows and you have no Windows image, the backend creates a standard Ubuntu droplet via the
  DO API, then runs a conversion script on it that downloads a Windows ISO, installs it
  unattended, injects VirtIO drivers, enables RDP, and sets a unique Administrator password.
  No prebuilt image or separate image link is required.
- **Tenancy:** single-tenant — one droplet per order.
- **Regions:** customer can pick from all ~8 DigitalOcean countries; region list fetched live
  from DO.
- **Auth:** none in this phase — endpoints are open.
- **Payments:** none in this phase — an order is marked paid manually, which triggers
  provisioning.
- **Scope:** backend API only. No admin dashboard, no customer portal, no storefront yet.

## How provisioning behaves (important expectations)
- **Not instant.** Each server takes roughly **20–45 minutes**: boot Ubuntu → download Windows
  ISO (~5 GB) → unattended install → driver injection → enable RDP → set password. Provisioning
  is therefore **asynchronous** — an order moves through states (queued → converting → active)
  and credentials (IP / Administrator / password) are delivered when it reaches `active`.
- A **status/progress endpoint** lets you (and later a customer) see where each server is in the
  conversion, including logs, so stuck conversions are visible.
- **Configurable ISO source per OS:** the script defaults to Microsoft's eval ISO for each OS,
  but each OS has a configurable ISO URL so you can point it at a stable source you control if
  Microsoft changes its links.

## Per-OS support reality
- **Windows Server 2019 / 2022 / 2025:** fully supported and reliable via the conversion script.
  Installs the **180-day evaluation**, which is fine for testing and demoing now. Before launch
  you convert these to your **SPLA-licensed** keys. These are the OSes you sell.

## What the API will let you do
- **Catalog:** list OS options (3 Server editions); list resource tiers; list 1/2/3-month
  durations; list DigitalOcean regions/countries (live).
- **Tier config:** add/edit resource tiers, each mapping to a DigitalOcean droplet size slug.
- **OS config:** per OS, set the ISO source URL used by the conversion script.
- **Pricing:** view and edit the sell price for each tier × duration.
- **Orders:** create an order (OS + tier + duration + region), view orders, mark an order paid.
  Marking paid kicks off the real DigitalOcean droplet creation + Windows conversion.
- **Servers:** list servers, view a server's details, RDP credentials, and live provisioning
  status/logs; take real lifecycle actions — reboot, rebuild, reset password, power off/suspend,
  destroy — against the live droplet. Each server tracks its expiry date from the plan duration.
- **Expiry handling:** servers past expiry are flagged/suspended for renewal or teardown.

## Deliverables beyond the API
- The **conversion script + unattended config per OS** (Server 2019/2022/2025 supported; Win10/11
  experimental), invoked automatically by the backend on each droplet.

## Explicitly out of scope for this phase (candidates for later)
- Prebuilt DigitalOcean custom images (chose the no-image conversion path instead).
- Multi-user RDS on shared droplets (needs RDS CALs / SPLA).
- User accounts / login (admin + customer).
- Online payments and self-service checkout.
- Customer-facing storefront and admin dashboard UI.
- Automatic renewals and billing.

## Licensing context (for your decisions, not built here)
- Single-tenant (one droplet per customer) stays within Windows' built-in 2-session allowance,
  avoiding RDS CALs. Server + SPLA is the compliant resale path.
- Windows 10/11 licenses forbid commercial multi-tenant hosting — not resale-eligible.
- The 180-day eval is for testing only; switch to SPLA-licensed Server before taking paying
  customers.

## What to confirm or push back on
- The **4 default resource tiers** (Starter/Standard/Pro/Power) and their specs — adjust or add
  your own.
- Accepting that provisioning is **~20–45 min, not instant**, because of the conversion step.
- API-only, no-auth, no-payments scope for now.
