# FirstLight — New Client Onboarding Runbook (v2)

v1 was written 2026-08-04 from the Pome pilot + tunnel migration and first
run on Potidea 2026-08-10. **v2 (2026-09-10)** reflects four user
decisions: our own PostgreSQL on Railway (no Supabase), our own login
system, no email channel at all (push + app only), and the tenancy
hierarchy **Group → Company (unique VAT) → Hotel**. The public app address
is **https://firstlight.hbis.io**.

Target state: the hotel's server runs **ONLY cloudflared** (a Windows
service). No code, no keys, no scheduled tasks on site. Everything else is
rows in our Postgres; all queries run from Railway through the tunnel.

Estimated effort: **~1–1.5 h total** — 15 min intake, 30 min Cloudflare +
server, 15 min database + users, 15 min verification (plus one overnight
check).

> **TODAY vs TARGET.** Until Phase C3 (own auth) and C4 (tenancy) ship —
> see [PHASE_C_RUNBOOK.md](PHASE_C_RUNBOOK.md) — Steps 2 and 3 still run
> against Supabase exactly as v1 did (hotels row + pms_config +
> hotel_users, Supabase Auth user). Keep the v2 intake answers (VAT, group,
> user list) in the client file so they load the day the tables go live.

---

## The hierarchy (read once)

| Level | Table | Key | Who sits here |
|---|---|---|---|
| Group | `groups` | slug | Optional. The same people own several companies. |
| Company | `organizations` | **VAT number** (unique per country) | The legal entity we invoice. Every hotel belongs to exactly one. |
| Hotel | `hotels` | id / slug | The property. Briefings, runs, feedback, watchlists all hang off this id. |
| User | `users` + `memberships` | email | A membership at ANY level grants every hotel below it (view `hotel_access`). |

Rules:
- **One company = one VAT.** Same VAT already exists → attach the new hotel
  to that company; never create a duplicate.
- **Same people own two companies** → create a `groups` row, point both
  companies' `group_id` at it, give the owners a group-level membership.
  They see every hotel of every company in one login.
- **A hotel changes hands** → `update hotels set org_id = <new company>`.
  Briefing history stays on the hotel id; nothing is deleted or copied.
- **Roles:** `owner` (company or group level) and `viewer` (hotel level —
  GM, revenue manager). HBIS staff = `users.is_platform_admin`, not a
  membership.
- **No email, ever.** Initial passwords are handed over by phone or in
  person; the app forces a password change on first login.

## Step 0 — Intake (ask the client, 15 min) — MANDATORY

Collect before touching anything:

| # | Question | Goes to | Why |
|---|---|---|---|
| 0.1 | **Company legal name + VAT number** (+ country) | `organizations` | Unique key. Existing VAT = existing company; the new hotel attaches to it. |
| 0.2 | **Same owners as another client of ours?** | `groups` | Decides whether a group row is created/reused so owners see all their hotels in one login. |
| 0.3 | **Real sellable inventory** (room count) | `hotels.total_rooms` | Never trust the PMS room list — out-of-order, dummy rooms and fake room types inflate it. All occupancy math divides by this. |
| 0.4 | **Season opening/closing dates, LAST year + THIS year** (seasonal hotels; city hotels = open all year → null) | `hotels.season_settings` jsonb (`docs/sql/2026-08-04_season_settings.sql`) | Correct occupancy over months that straddle opening/closing; powers the closed-season briefing (next-year OTB vs STLY). |
| 0.5 | **Hotel type**: `resort` or `city` | `pms_config.hotel_type` | Selects the lead-time bucket profile for Signal 3. |
| 0.6 | **App users**: name, email, level (owner at company/group, viewer at hotel) | `users` + `memberships` | Individual accounts, never shared credentials. Email is the login name only — nothing is sent to it. |
| 0.7 | **Briefing language**: EN or Greek | `users.language` + `hotel_prefs` (app can change it later) | Narration language for cards + hero. |
| 0.8 | PMS type + version (today: Protel MSSQL) | `hotels.pms_type` | Adapter selection (`get_adapter`). |
| 0.9 | **SQL Server details**: LAN IP, port (1433), database name (`bidata`), and the **mpehotel id** of this property | `pms_config.sql` | Multi-property Protel DBs hold several hotels — `pms_hotel_id` (mpehotel) selects the right one. Verify with a COUNT query per mpehotel against known bookings. |
| 0.10 | IT contact who can RDP the PMS server | — | Needed for Step 1 + the read-only login. |

Gone from v1: GM recipient email. `hotels.recipient_email` /
`recipient_name` are deprecated columns — leave them empty.

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

**Opera 5 / Oracle variant (first run: Tor group, 2026-09-11):** route
`sql-<group>.hbis.io → tcp://<oracle-ip>:1521` (ONE tunnel + token for the
whole multiproperty server); read-only Oracle account with a non-expiring
profile (`CREATE PROFILE ... PASSWORD_LIFE_TIME UNLIMITED`) and SELECT on the
OPERA views; check `USE_SHARED_SOCKET` on the 19c home only if the tunnel
connect hangs (Tor: not needed). Pre-flight from the laptop:
`ORA_PW=... py -3.13 scripts/validate_opera.py [host:port]` — expect
`complete=True`, yesterday == `RESERVATION_STAT_DAILY`, full-year revenue ==
the group's Hotel BI table. One `hotels` row per RESORT code (Step 2).

## Step 2 — Database + users (15 min) — our Postgres on Railway

**TODAY (until C3/C4):** Supabase, as v1 — `hotels` row + `pms_config`,
Supabase Auth user, `hotel_users` mapping. Skip the group/company/VAT
rows; keep the answers.

**TARGET:** one admin script, `scripts/onboard_hotel.py` (TO-DO), takes
the intake sheet and does the following in one transaction. Manual SQL
recipes for each line are at the bottom of
`docs/sql/pg/2026-09-10_tenancy_auth.sql`.

1. **Company:** look up `organizations` by (country, vat_number) → reuse;
   else insert name, legal_name, vat_number, country, slug.
2. **Group** (only if 0.2 = yes): reuse or insert `groups`; set
   `organizations.group_id` on every company involved.
3. **Hotel:** insert `hotels` (org_id, name, slug, total_rooms from 0.3,
   timezone, pms_type, season_settings from 0.4, `api_token` = 32 random
   bytes, pms_config):
   ```jsonc
   {
     "fetch_mode": "tunnel",
     "tunnel_hostname": "sql-<hotel>.hbis.io",
     "cf_access_client_id": "<token id>",
     "cf_access_client_secret": "<token secret>",
     "hotel_type": "resort",              // from 0.5
     "language": "en",                    // from 0.7
     "sql": { "database": "bidata", "user": "firstlight_ro",
              "password": "<...>", "pms_hotel_id": <mpehotel> }
   }
   ```
4. **Users:** for each person in 0.6 → `users` row (email lower-cased,
   argon2id hash of a generated initial password,
   `must_change_password = true`, language) + a `memberships` row at the
   right level (`org`/`group` + `owner`, or `hotel` + `viewer`). Existing
   email → membership only.
5. **Handover sheet** (printed by the script, never emailed): hotel id,
   app address https://firstlight.hbis.io, one initial password per user.
   Read the passwords out by phone; do not paste them into chats that
   persist.

## Step 3 — Verify (15 min + overnight)

1. Insert a `refresh_commands` row for the new hotel (see `ops-monitoring`;
   TODAY Supabase, TARGET Postgres where LISTEN/NOTIFY picks it up).
   Expect in `refresh_runs`: `status: success`, `fetch_path: "tunnel"`,
   `data_quality.complete: true`, `rows_fetched` populated **including
   signal fields** (pickup_daily, cancel_daily, lead_time,
   consumed_by_source, pace_next_year).
2. Log in at **https://firstlight.hbis.io** as one of the NEW users, not
   your admin account: forced password change works; the hotel appears in
   the picker (an owner also sees every other hotel of the company/group);
   the briefing renders; numbers eyeballed against the PMS (yesterday
   revenue, month OTB).
3. Reconciliation spot-check: Pickup boxes == butterfly totals; hero numbers
   == KPI cards (manual refreshes reuse the day's AI, so run one).
4. Enable push on the user's phone (bell) → `POST /push/test` arrives.
5. Next 03:30 UTC scheduled run: exactly ONE push arrives and NO email;
   check `cards_audit` (attempts=1, no validation problems, fallback rate
   low).

## Step 4 — Merges and moves (admin, any time)

| Situation | Action | Data impact |
|---|---|---|
| Two companies, same owners | insert `groups`; set `group_id` on both; owners get a `group` membership | none — hotels keep their company + history |
| Hotel sold / moved to another company | `update hotels set org_id = <new>` | briefings, runs, feedback, watchlists stay on the hotel id |
| Two companies turn out to be ONE legal entity (same VAT) | move hotels (`org_id`) and memberships to the surviving company; set the other `organizations.active = false` | never delete the losing row |
| User leaves | `users.active = false`; delete their `sessions` | feedback / watchlist rows keep the user id |

## Step 5 — Decommission legacy (migrating hotels only)

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
- Same VAT, different trading name = same company. Ask for the VAT first,
  the brand second.
- Full incident history: `ENGINEERING_LOG.md` §6.

---

## Potidea Palace — worksheet (first run of v1, 2026-08-10)

Facts (verified 2026-08-04, onboarded tunnel-direct 2026-08-10):
- OWN SQL Server: `192.20.10.8`, database `BiData`, **mpehotel = 1**
  (user-confirmed; each server numbers its own properties — the old .env's
  `HOTEL_ID=2` was the zero-data incident). Old login `sa` → replace with
  `firstlight_ro`.
- Cloudflared tunnel ALREADY runs on the hotel server (serves
  `potidea-data.hbis.io`) → TCP public hostname `sql-potidea.hbis.io` →
  `tcp://192.20.10.8:1433` + Access Service Auth token `railway-potidea-sql`.
- Supabase row exists (236 rooms), users mapped in `hotel_users`,
  `recipient_email` empty (now permanently irrelevant — no email channel).
- NOT in Pome's DB (Pome's bidata holds only its own mpehotel=1).

Still to collect: real sellable inventory (236?) ✚ season dates 2025+2026
✚ company VAT + legal name ✚ whether Pome and Potidea share owners (group?)
✚ firstlight_ro password.

Also fix while in there (both hotels): swap Pome's `sql.user` from `sa` to
`firstlight_ro` (Step 1.2) — long-standing security must-fix.
