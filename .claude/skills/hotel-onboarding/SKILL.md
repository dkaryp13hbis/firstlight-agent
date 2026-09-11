---
name: hotel-onboarding
description: Adding a new hotel or migrating an existing one to the tunnel-direct path — Cloudflare setup, Group→Company(VAT)→Hotel tenancy, users on our own login, pms_config shape, verification steps, and decommissioning the hotel server.
---

# Hotel onboarding / tunnel migration

**Canonical runbook: `docs/ONBOARDING.md` (v2, 2026-09-10)** — intake
form → cloudflared → database + users → verify → merges → decommission,
with the Potidea worksheet. This skill holds the condensed task knowledge;
the runbook is what you follow.

Target state: hotel server runs ONLY cloudflared (Windows service). No code,
no keys, no scheduled tasks. Everything else is rows in OUR Postgres on
Railway. Public app: **https://firstlight.hbis.io**. No email channel —
push + app only.

**TODAY vs TARGET:** until Phase C3 (own auth) + C4 (tenancy) ship
(`docs/PHASE_C_RUNBOOK.md`), the database step still runs in Supabase
(hotels row + pms_config + Supabase Auth user + hotel_users). Collect the
v2 intake answers anyway.

## 0. Tenancy (decided 2026-09-10)

Group (shared owners, optional) → Company = `organizations` (**unique VAT
per country**) → Hotel. Users = `users` + `memberships` at any level; the
`hotel_access` view resolves access. Roles: `owner` (company/group),
`viewer` (hotel). Merges never delete: same owners → `groups` row; hotel
changes hands → `update hotels set org_id`. DDL + admin recipes:
`docs/sql/pg/2026-09-10_tenancy_auth.sql`.

## 1. Cloudflare (one-time per hotel, ~30 min, all remote)

1. Zero Trust → Tunnels: hotel already has (or gets) a named tunnel
   (e.g. `FL_pome`) running as a Windows service on the hotel server.
2. Tunnel → Public hostname: route `sql-<hotel>.hbis.io` →
   `tcp://<lan-ip-of-sql-server>:1433`.
3. Access → Applications → Add self-hosted app for that hostname; policy
   Action = **Service Auth** (NOT Allow), Include = Service Token; create token
   `railway-<hotel>-sql` and save its Client ID/Secret.

## 2. Hotel row (`hotels`, under its company)

```jsonc
pms_type: "protel_mssql",
pms_config: {
  "fetch_mode": "tunnel",
  "tunnel_hostname": "sql-<hotel>.hbis.io",
  "cf_access_client_id": "<token id>",
  "cf_access_client_secret": "<token secret>",
  "hotel_type": "resort",            // or "city" — lead-time bucket profile
  "language": "en",
  "sql": { "database": "bidata", "user": "firstlight_ro",
           "password": "<...>", "pms_hotel_id": <mpehotel> }
}
```
**Opera 5 / Oracle (multiproperty, one tunnel per server):**
```jsonc
pms_type: "opera_oracle",
pms_config: {
  "fetch_mode": "tunnel",
  "tunnel_hostname": "sql-<group>.hbis.io",        // shared by every property
  "cf_access_client_id": "...", "cf_access_client_secret": "...",
  "hotel_type": "city", "language": "en",
  "sql": { "database": "opera",                     // Oracle SERVICE name
           "user": "opera_ro", "password": "<...>",
           "pms_hotel_id": "CITY" }                  // Opera RESORT code
}
```
One `hotels` row per RESORT code; tunnel route = `tcp://<oracle-ip>:1521`;
read-only account needs SELECT on the OPERA views (the hotel's DBA usually
has a role for that, e.g. `OPERA_RO_ROLE`). Pre-flight from the laptop with
`scripts/validate_opera.py`.

Also: name, total_rooms, season_settings, api_token. `recipient_email` /
`recipient_name` are deprecated — leave empty. Prefer a read-only SQL
login (`CREATE LOGIN firstlight_ro` + `GRANT SELECT ON SCHEMA::proteluser`
in `bidata` AND `protel`) — never ship `sa` beyond a pilot.

**MANDATORY intake (user-mandated 2026-08-04, extended 2026-09-10):**
- **Company legal name + VAT** (+ country) — existing VAT = existing company.
- **Same owners as another client?** — decides the `groups` row.
- **Available inventory** — the real sellable room count for `total_rooms`.
  Ask the hotel; do NOT trust the PMS room list. Occupancy math divides by it.
- **Season settings** — seasonal hotels: opening/closing dates LAST + THIS
  year → `hotels.season_settings` jsonb. City hotels: `null`.
- **App users** — name, email (login name only), level. Individual
  accounts. Initial password handed over by phone; forced change on first
  login. Nothing is ever emailed.
- Hotel type, language, PMS + SQL details incl. **mpehotel id**, IT contact.

## 3. Verify (before decommissioning anything)

1. Insert a refresh command (see `ops-monitoring`) → expect a `refresh_runs`
   row with `fetch_path: "tunnel"`, `data_quality.complete: true`, and
   `rows_fetched` populated incl. signal fields (the tunnel path runs the
   full Q1–Q16 pack even if the hotel's old bridge never did).
2. Log in at firstlight.hbis.io as a NEW user (not admin): forced password
   change, hotel in picker (owner sees the whole company/group), numbers
   sane vs PMS. Bell → `POST /push/test` arrives.
3. Next 03:30 UTC scheduled run: exactly ONE push, NO email.

## 4. Decommission the hotel server (existing hotels)

Kill the daemon process; disable Task Scheduler tasks ("FirstLight Morning
Briefing", "FirstLight Refresh Daemon"); keep the code folder ~1 week as
rollback, then delete. RDP: use Disconnect, NOT Sign out (sign-out kills
manually started processes — cause of a real incident).

## Gotchas from the first two onboardings

- Multi-property Protel DBs: `pms_hotel_id` (mpehotel) selects the property.
- Wrong server / stray repo copies: check you're on the right machine before
  touching processes.
- Same VAT, different trading name = same company. Ask VAT first.
- Full incident list: `docs/ENGINEERING_LOG.md` §6; deeper detail in the
  user-level memory `hotel_onboarding_checklist` / `known_errors_and_fixes`.
