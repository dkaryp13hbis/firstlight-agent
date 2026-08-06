---
name: hotel-onboarding
description: Adding a new hotel or migrating an existing one to the tunnel-direct path — Cloudflare setup, pms_config shape, hotels row, verification steps, and decommissioning the hotel server.
---

# Hotel onboarding / tunnel migration

**Canonical runbook: `docs/ONBOARDING.md`** (intake form → cloudflared →
Supabase → verify → decommission, with the Potidea worksheet). This skill
holds the condensed task knowledge; the runbook is what you follow.

Target state: hotel server runs ONLY cloudflared (Windows service). No code,
no keys, no scheduled tasks. Everything else is config in Supabase.

## 1. Cloudflare (one-time per hotel, ~30 min, all remote)

1. Zero Trust → Tunnels: hotel already has (or gets) a named tunnel
   (e.g. `FL_pome`) running as a Windows service on the hotel server.
2. Tunnel → Public hostname: route `sql-<hotel>.hbis.io` →
   `tcp://<lan-ip-of-sql-server>:1433`.
3. Access → Applications → Add self-hosted app for that hostname; policy
   Action = **Service Auth** (NOT Allow), Include = Service Token; create token
   `railway-<hotel>-sql` and save its Client ID/Secret.

## 2. Supabase hotels row

```jsonc
pms_type: "protel_mssql",
pms_config: {
  "fetch_mode": "tunnel",
  "tunnel_hostname": "sql-<hotel>.hbis.io",
  "cf_access_client_id": "<token id>",
  "cf_access_client_secret": "<token secret>",
  "hotel_type": "resort",            // or "city" — lead-time bucket profile
  "sql": { "database": "bidata", "user": "firstlight_ro",
           "password": "<...>", "pms_hotel_id": <mpehotel> }
}
```
Also: name, total_rooms, recipient_email/name. Prefer a read-only SQL login
(`CREATE LOGIN firstlight_ro` + `GRANT SELECT ON SCHEMA::proteluser` in
`bidata` AND `protel`) — never ship `sa` beyond a pilot.

**MANDATORY intake questions (user-mandated 2026-08-04):**
- **Available inventory** — the real sellable room count for `total_rooms`.
  Ask the hotel; do NOT trust the PMS room list (out-of-order rooms, dummy
  rooms and fake room types inflate it). Occupancy math divides by this.
- **Season settings** — for seasonal hotels: opening and closing dates for
  LAST year and THIS year → `hotels.season_settings` jsonb
  (`docs/sql/2026-08-04_season_settings.sql`, e.g.
  `{"2025": {"open": "...", "close": "..."}, "2026": {...}}`).
  Without them, occupancy over a month that straddles opening/closing uses
  calendar days and understates; the closed-season hero (Q16
  `pace_next_year`, next-year OTB vs STLY) also keys off the season window.
  City hotels: `null` (open all year).

## 3. Verify (before decommissioning anything)

1. Insert a refresh command (see `ops-monitoring`) → expect a `refresh_runs`
   row with `fetch_path: "tunnel"`, `data_quality.complete: true`, and
   `rows_fetched` populated (incl. signal fields — the tunnel path runs the
   full Q1–Q13 pack even if the hotel's old bridge never did).
2. Check the briefing renders in the PWA and the numbers look sane vs PMS.
3. Next 03:30 UTC scheduled run: exactly ONE email + ONE push.

## 4. Decommission the hotel server (existing hotels)

Kill the daemon process; disable Task Scheduler tasks ("FirstLight Morning
Briefing", "FirstLight Refresh Daemon"); keep the code folder ~1 week as
rollback, then delete. RDP: use Disconnect, NOT Sign out (sign-out kills
manually started processes — cause of a real incident).

## Gotchas from the first two onboardings

- Multi-property Protel DBs: `pms_hotel_id` (mpehotel) selects the property.
- Wrong server / stray repo copies: check you're on the right machine before
  touching processes (a stray copy in C:\Users\Administrator once confused a
  session).
- Full incident list: `docs/ENGINEERING_LOG.md` §6; deeper detail in the
  user-level memory `hotel_onboarding_checklist` / `known_errors_and_fixes`.
