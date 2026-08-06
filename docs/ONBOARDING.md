# FirstLight — New Client Onboarding Runbook (v1)

The canonical step-by-step for adding a hotel. Written 2026-08-04, distilled
from the Pome pilot + tunnel migration. Potidea Palace is the first hotel to
be onboarded with this runbook, treated as a brand-new client.

Target state: the hotel's server runs **ONLY cloudflared** (a Windows
service). No code, no keys, no scheduled tasks on site. Everything else is
configuration in Supabase; all queries run from Railway through the tunnel.

Estimated effort: **~1–1.5 h total** — 15 min intake, 30 min Cloudflare +
server, 15 min Supabase, 15 min verification (plus one overnight check).

---

## Step 0 — Intake (ask the client, 15 min) — MANDATORY

Collect before touching anything:

| # | Question | Goes to | Why |
|---|---|---|---|
| 0.1 | **Real sellable inventory** (room count) | `hotels.total_rooms` | Never trust the PMS room list — out-of-order, dummy rooms and fake room types inflate it. All occupancy math divides by this. |
| 0.2 | **Season opening/closing dates, LAST year + THIS year** (seasonal hotels; city hotels = open all year → null) | `hotels.season_settings` jsonb (`docs/sql/2026-08-04_season_settings.sql`) | Correct occupancy over months that straddle opening/closing; powers the closed-season briefing (next-year OTB vs STLY). |
| 0.3 | **Hotel type**: `resort` or `city` | `pms_config.hotel_type` | Selects the lead-time bucket profile for Signal 3. |
| 0.4 | GM/recipient **name + email** | `hotels.recipient_email/name` | Morning email + push target. |
| 0.5 | **Briefing language**: EN or Greek | `pms_config.language` (app can change it later via hotel_prefs) | Narration language for cards + hero. |
| 0.6 | PMS type + version (today: Protel MSSQL) | `hotels.pms_type` | Adapter selection (`get_adapter`). |
| 0.7 | **SQL Server details**: LAN IP, port (1433), database name (`bidata`), and the **mpehotel id** of this property | `pms_config.sql` | Multi-property Protel DBs hold several hotels — `pms_hotel_id` (mpehotel) selects the right one. Verify with a COUNT query per mpehotel and compare against known bookings. |
| 0.8 | IT contact who can RDP the PMS server | — | Needed for Step 1 + the read-only login. |
| 0.9 | App users: who gets access (emails) | Supabase auth + `hotel_users` | Individual accounts preferred over shared credentials. |

## Step 1 — Hotel server: cloudflared + read-only SQL login (30 min, remote w/ IT)

1. **Cloudflared tunnel** (skip if the site already has one — a second hotel
   on the same SQL server reuses the existing tunnel):
   - Zero Trust → Tunnels → create named tunnel `FL_<hotel>`; install as a
     **Windows service** on a machine that can reach the SQL server.
   - Public hostname: `sql-<hotel>.hbis.io` → `tcp://<sql-lan-ip>:1433`.
   - Access → Applications → self-hosted app for that hostname; policy
     Action = **Service Auth** (NOT Allow); create service token
     `railway-<hotel>-sql`; save Client ID + Secret.
2. **Read-only SQL login** (never ship `sa` beyond a pilot):
   ```sql
   CREATE LOGIN firstlight_ro WITH PASSWORD = '<strong-pw>';
   USE bidata;  CREATE USER firstlight_ro FOR LOGIN firstlight_ro;
                GRANT SELECT ON SCHEMA::proteluser TO firstlight_ro;
   USE protel;  CREATE USER firstlight_ro FOR LOGIN firstlight_ro;
                GRANT SELECT ON SCHEMA::proteluser TO firstlight_ro;
   ```
3. Nothing else goes on the hotel server. No repo clone, no .env, no tasks.

## Step 2 — Supabase (15 min)

1. `hotels` row (insert new, or update if migrating):
   - `name`, `total_rooms` (from 0.1!), `recipient_email`, `recipient_name`,
     `active: true`, `season_settings` (from 0.2), `pms_type: "protel_mssql"`,
   - `pms_config`:
     ```jsonc
     {
       "fetch_mode": "tunnel",
       "tunnel_hostname": "sql-<hotel>.hbis.io",
       "cf_access_client_id": "<token id>",
       "cf_access_client_secret": "<token secret>",
       "hotel_type": "resort",              // from 0.3
       "language": "en",                    // from 0.5
       "sql": { "database": "bidata", "user": "firstlight_ro",
                "password": "<...>", "pms_hotel_id": <mpehotel> }
     }
     ```
2. Users: create Supabase auth users (or reuse) + `hotel_users` rows mapping
   each user to the new hotel id.
3. Confirm the `briefings_hotel_date_unique` constraint exists (one-time,
   already in place on this project).

## Step 3 — Verify (15 min + overnight)

1. Insert a `refresh_commands` row for the new hotel (see `ops-monitoring`).
   Expect in `refresh_runs`: `status: success`, `fetch_path: "tunnel"`,
   `data_quality.complete: true`, `rows_fetched` populated **including signal
   fields** (pickup_daily, cancel_daily, lead_time, consumed_by_source,
   pace_next_year).
2. Open the PWA with a mapped user → briefing renders, hotel appears in the
   picker, numbers eyeballed against the PMS (yesterday revenue, month OTB).
3. Reconciliation spot-check: Pickup boxes == butterfly totals; hero numbers
   == KPI cards (manual refreshes reuse the day's AI, so run one).
4. Next 03:30 UTC scheduled run: exactly ONE email + ONE push arrives; check
   `cards_audit` (attempts=1, no validation problems, fallback rate low).

## Step 4 — Decommission legacy (migrating hotels only)

Kill the old daemon process; disable Task Scheduler tasks ("FirstLight
Morning Briefing", "FirstLight Refresh Daemon"); keep the code folder ~1 week
as rollback, then delete. RDP rule: **Disconnect, never Sign out** (sign-out
kills manually started processes — caused a real incident).

## Gotchas (earned the hard way)

- Multi-property DBs: wrong `pms_hotel_id` = silently plausible wrong data.
  Validate with a known number (e.g. yesterday's room nights) before go-live.
- Check you're on the right machine before touching processes; stray repo
  copies have confused sessions before.
- The tunnel path runs the FULL query pack (Q1–Q16) even if the hotel's old
  bridge never did — first refresh may reveal PMS data quirks (fake room
  types, comp bookings). `_FAKE_RT_EXCLUDE` handles the known ones.
- Full incident history: `ENGINEERING_LOG.md` §6.

---

## Potidea Palace — worksheet (first run of this runbook)

Known today:
- `hotels` row exists (id in Supabase), 236 rooms (**confirm real sellable
  count — 0.1**), recipient set, currently legacy (`pms_type: "protel"`, no
  pms_config) — old bridge still running on the hotel server.
- Same hotel group as Pome. **Open question 0.7: is Potidea in the SAME
  Protel `bidata` database as Pome (different mpehotel) or its own SQL
  server?** If same server → reuse the `sql-pome.hbis.io` tunnel + token,
  and Step 1 shrinks to just the mpehotel lookup (~5 min).

To collect (intake): real inventory ✚ season dates 2025+2026 ✚ hotel_type
(resort) ✚ language ✚ mpehotel ✚ SQL server location ✚ app users.

Also fix while in there (both hotels): swap Pome's `sql.user` from `sa` to
`firstlight_ro` (Step 1.2) — long-standing security must-fix.
