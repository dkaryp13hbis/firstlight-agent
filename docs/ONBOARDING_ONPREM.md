# Onboarding — on-premises PMS (tunnel)

**Audience:** HBIS engineering. **Part of:** [ONBOARDING.md](ONBOARDING.md)
Step 2 — do Steps 0–1 there first, come back to Step 3 there afterwards.
**Keep it current:** a new on-premises adapter, a change to the tunnel
set-up or to `pms_config` updates this file in the same commit.
Last verified 2026-10-28 (live: Pome + Potidea on Protel, Tor group on Opera 5).

## What runs where

The hotel's server runs **only cloudflared** (a Windows service, outbound
only — nothing at the hotel listens to the internet). Every query runs from
our Railway container, through a Cloudflare tunnel, straight into the PMS
database, with a read-only login. No code, keys, files or scheduled tasks on
the hotel's machine.

| PMS | Database | Port | `pms_type` | Status |
|---|---|---|---|---|
| Protel | SQL Server (`bidata`) | 1433 | `protel_mssql` | live |
| Opera 5 | Oracle 19c (OPERA schema) | 1521 | `opera_oracle` | live (multiproperty) |
| Pylon | SQL Server | 1433 | `pylon_mssql` | planned — adapter not built |
| Fidelio V8 | Oracle | 1521 | `fidelio_oracle` | planned — adapter not built |

A planned PMS needs its adapter first (`db/adapters/<pms_type>/`, filling
the HotelDataSnapshot contract; skill `pms-queries`).

## Intake extras (add to ONBOARDING.md Step 0)

| # | Question | Goes to |
|---|---|---|
| P.1 | PMS + version; database engine | `pms_type` |
| P.2 | Database server LAN IP + port; database name (Protel `bidata`; Opera: the Oracle **service name**) | tunnel route, `pms_config.sql` |
| P.3 | **The property's id inside the PMS**: Protel `mpehotel` number, Opera **RESORT code** | `pms_config.sql.pms_hotel_id` |
| P.4 | A machine on that LAN that can run cloudflared as a service (usually the DB server) | Step A |
| P.5 | IT contact who can log in (RDP) and create a database login | Steps A–B |

## Step A — Cloudflare tunnel (with the hotel's IT, ~20 min)

Skip the tunnel itself when the server already has one (a second property
on the same server reuses it; Tor's three Opera properties share one).

1. Cloudflare Zero Trust → **Networks → Tunnels** → create a named tunnel
   `FL_<hotel>`; IT installs it **as a Windows service** on the P.4 machine
   (the dashboard shows the one-line install command).
2. Tunnel → **Public hostname**: `sql-<hotel>.hbis.io` →
   `tcp://<db-lan-ip>:1433` (Oracle: `:1521`).
3. **Access → Applications** → self-hosted app for that hostname; policy
   **Action = Service Auth** (NOT Allow); create the service token
   `railway-<hotel>-sql`; keep the Client ID + Secret for Step D.
4. Dashboard check: the tunnel is **Healthy**, 1 replica, the connector
   version is current (Cloudflare advisories: upgrade with
   `cloudflared update` + `Restart-Service cloudflared`, outside run times).

## Step B — Read-only database login (IT, ~10 min)

Never connect with `sa` / a DBA account beyond a pilot (Pome and Potidea
still do — top item on the security list).

**Protel (SQL Server):**
```sql
CREATE LOGIN firstlight_ro WITH PASSWORD = '<strong-pw>';
USE bidata;  CREATE USER firstlight_ro FOR LOGIN firstlight_ro;
             GRANT SELECT ON SCHEMA::proteluser TO firstlight_ro;
USE protel;  CREATE USER firstlight_ro FOR LOGIN firstlight_ro;
             GRANT SELECT ON SCHEMA::proteluser TO firstlight_ro;
```

**Opera 5 (Oracle):** a read-only account (Tor: `opera_ro`) with a
non-expiring profile (`CREATE PROFILE … PASSWORD_LIFE_TIME UNLIMITED`) and
SELECT on the OPERA views — the hotel's DBA usually has a role for it.
Schema **OPERA**, not TRAINING.

The password travels by phone, like user passwords.

## Step C — Pre-flight from the laptop (before any production config)

- **Opera:** `ORA_PW=... py -3.13 scripts/validate_opera.py [host:port]` —
  expect `complete=True`, yesterday equal to `RESERVATION_STAT_DAILY`, and
  full-year revenue equal to the group's Hotel Bi table.
- **Protel:** open the tunnel locally (`cloudflared access tcp`, see
  `db/tunnel.py`) and count yesterday's room nights per `mpehotel`; the
  right id matches a number the hotel gives you. Potidea's old config
  pointed at the wrong id once — zero-data incident.
- The laptop's own cloudflared must be current (2026-10-28: still 2026.6.0
  — upgrade by hand with the MSI).

## Step D — The `pms_config` block (hand it to ONBOARDING.md Step 3)

**Protel:**
```jsonc
{
  "fetch_mode": "tunnel",
  "tunnel_hostname": "sql-<hotel>.hbis.io",
  "cf_access_client_id": "<token id>",
  "cf_access_client_secret": "<token secret>",
  "hotel_type": "resort",            // 0.5 — or "city"
  "language": "en",                  // 0.7
  "sql": { "database": "bidata", "user": "firstlight_ro",
           "password": "<...>", "pms_hotel_id": <mpehotel> }
}
```

**Opera 5** — one hotel row per RESORT code, the same tunnel block on each:
```jsonc
{
  "fetch_mode": "tunnel",
  "tunnel_hostname": "sql-<group>.hbis.io",
  "cf_access_client_id": "...", "cf_access_client_secret": "...",
  "hotel_type": "city", "language": "en",
  "briefing_slot": "early",          // optional: 05:00 Athens instead of 03:30 UTC
  "sql": { "database": "<service name>", "user": "opera_ro",
           "password": "<...>", "pms_hotel_id": "<RESORT code>" }
}
```

Room count (0.3): Opera = the non-pseudo room categories, confirmed with the
hotel. Then continue with [ONBOARDING.md](ONBOARDING.md) Step 3.

## Step E — Moving a hotel off the old on-site daemon (legacy only)

Stop the old daemon process; disable the Task Scheduler tasks "FirstLight
Morning Briefing" and "FirstLight Refresh Daemon"; keep the code folder a
week as rollback, then delete it. RDP: **Disconnect, never Sign out** —
signing out kills hand-started processes (a real incident).

## Gotchas (earned the hard way)

- **Wrong property id = plausible wrong data.** Validate one known number
  before go-live (Step C).
- **Touching Cloudflare Access for hotel N can break hotel N−1.** An RDP
  "browser rendering" rule on the Pome app 403'd the tunnel (2026-08-10).
  After any Access change, test every hotel's hostname.
- **First tunnel run shows PMS quirks** (fake room types, comp bookings):
  `_FAKE_RT_EXCLUDE` handles the known Protel ones.
- **Opera revenue includes posting-master (pseudo room) postings, nights
  exclude them**; never filter `psuedo_room_yn` in the WHERE of the BI view
  (27 s vs 2 s).
- Check you are on the right machine before touching processes.
- Full incident history: ENGINEERING_LOG §6.

## Potidea Palace — worksheet (first tunnel onboarding, 2026-08-10)

- Own SQL Server `192.20.10.8`, database `BiData`, **mpehotel = 1** (each
  server numbers its own properties — the old `HOTEL_ID=2` was the
  zero-data incident). Login still `sa` → replace with `firstlight_ro`.
- Reused the existing tunnel: TCP hostname `sql-potidea.hbis.io` →
  `tcp://192.20.10.8:1433` + Service Auth token `railway-potidea-sql`.
- Still open: real sellable rooms (236?), season dates, company VAT +
  legal name, shared owners with Pome?, `firstlight_ro` for both hotels.

## Change history

| Date | Change | Commit |
|---|---|---|
| 2026-08-04 → 09-11 | Steps lived in ONBOARDING.md v1 / v2 (Protel, then the Opera variant) | — |
| 2026-10-28 | Own runbook: adapter table, intake extras, tunnel / login / pre-flight / pms_config steps, gotchas, Potidea worksheet | (this commit) |
