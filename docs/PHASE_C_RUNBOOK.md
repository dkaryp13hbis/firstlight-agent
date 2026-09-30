# Phase C — Supabase → Railway Postgres migration runbook

Written 2026-09-04, BEFORE execution (CTO discipline: plan first, then touch).
Preconditions status is tracked at the bottom. Do not start C1 until every
precondition is ✅.

## Why

1. Prerequisite for cloud PMS (Opera Cloud / Hotelizer): the reservation
   store + incremental sync engine lives in OUR Postgres.
2. Cost/control at scale (10+ hotels): storage next to the processor, no
   REST hop, no Supabase tier pressure.
3. LISTEN/NOTIFY replaces the 30s refresh_commands poller.
4. **2026-09-10 (user):** Supabase goes away COMPLETELY — including Auth.
   We run our own login system (C3) and the tenancy hierarchy
   Group → Company (unique VAT) → Hotel with users at any level (C4).
   No email channel at all: push + app only (C5).

## What moves — full inventory (as of 2026-09-04, 736 rows total)

| Table | Rows | Written by | Read by app directly? | Phase |
|---|---|---|---|---|
| briefings | 204 | backend | YES (latest, by-date, history) | C1 + C2 |
| refresh_runs | 347 | backend | YES (Data health) | C1 + C2 |
| refresh_commands | 144 | backend + app (refresh btn) | writes YES | C1 + C2 |
| hotels | 4 | ops | YES (names via hotel_users) | C1 + C2 |
| hotel_users | 4 | ops | YES (membership) | C1 + C2 |
| organizations | 1 | ops | no | C1 |
| insight_feedback | 3 | app | YES (write) | C2 |
| hotel_prefs | 3 | app | YES (write) | C2 |
| push_subscriptions | 2 | app | YES (r/w) | C2 |
| usage_events | 18+ | app | write-only | C2 |
| intraday_log | 2+ | backend | no | C1 |
| watchlist | 4 | app | YES (r/w) | C2 |
| (auth.users) | — | Supabase Auth | login | C3 — imported into OUR `users` table with the SAME uuid; own login system (decided 2026-09-10) |

## Strategy: three separable phases

### C1 — backend-owned storage (2–3 days, invisible to users)
Move what only the BACKEND touches; the app keeps reading Supabase until C2.
1. ✅ 2026-09-05 PROVISIONED: service "Postgres" (PG 18) in project
   zucchini-friendship / env cloudflare — PRIVATE-ONLY (no public access;
   reach it via `railway connect Postgres --tunnel-only`; CLI logged in as
   d.karypidis@hbis.io, SSH key firstlight-dev registered; DB password in
   C:\FirstLightBackups\pg.env). ⬜ user: add the DATABASE_URL reference
   to the web service's Variables (Add Reference), leave STORAGE unset.
2. Schema: ✅ WRITTEN 2026-09-04 — `docs/sql/pg/schema.sql` (all 12 tables,
   derived from the live column inventory + every docs/sql constraint;
   includes the LISTEN/NOTIFY trigger for refresh_commands and the
   count-verification query; no RLS by design — service-only access, the
   app goes through the API). Apply with:
   psql "$DATABASE_URL" -f docs/sql/pg/schema.sql
   ✅ APPLIED 2026-09-05 on the real instance via tunnel: PG 18.6, all 12
   tables + every unique/PK constraint + refresh_commands_notify trigger
   verified from information_schema/pg_constraint.
3. `db/store.py`: ✅ WRITTEN + WIRED (dormant) 2026-09-04 — psycopg3 pool
   (max 5, lazy import: production untouched until the env flip), modes
   STORAGE=supabase (default, no-op) | dual (write both) | pg (read PG).
   Hooks live at the 4 backend write sites: cloud_push briefing upsert,
   RunLogger insert+patch (reuses the Supabase run id so stores stay
   joinable), intraday claim mirror. Also mirror_rows(table, rows) +
   counts() for step 4's nightly verify, and read functions ready for
   step 5. test_store.py: 12 dormant-mode checks incl. the lazy-import
   guarantee. psycopg[binary,pool]==3.2.3 added to requirements (Railway
   image grows slightly on next deploy — inert until STORAGE is set).
4. **Dual-write window (3 days)** — ✅ OPENED 2026-09-04 ~21:10 UTC
   (STORAGE=dual + DATABASE_URL on the web service). Day 1 verified by
   hand 2026-09-05: 03:30 briefings + runs in BOTH stores, values equal;
   full backup mirrored (749 rows ALL MATCH). Now AUTOMATED:
   - 07:20 UTC server-side `run_dual_verify` (audit.py): per-table counts
     + latest-briefing equality, ops email only on drift (db4e1d7)
   - 08:40 local `FirstLightMirror` task (scripts/mirror_to_pg.ps1):
     mirrors the day's backup into PG through the CLI tunnel — keeps
     app-written tables synced until C2 (tested: 749 rows)
   Window counts from 2026-09-05; read-flip candidate 2026-09-08+.
   HEAD START ✅ 2026-09-05: the FULL 2026-09-04 backup (736 rows, all 12
   tables) was mirrored into PG through store.mirror_rows — counts ALL
   MATCH the backup. So the initial restore is done and rehearsed; the
   dual-write window only needs to prove the DELTAS. (Re-mirror once more
   right before enabling STORAGE=dual to catch up the gap days.)
5. Flip backend reads to PG (`STORAGE=pg`). Supabase still gets writes
   (for the app + rollback).
6. Queue: replace refresh_commands polling with LISTEN/NOTIFY; the app
   still INSERTs into Supabase → a bridge poller forwards to PG until C2.

### C2 — app traffic through the API (3–4 days)
The app stops talking to Supabase for DATA (auth stays).
1. ✅ SHIPPED 2026-09-04/05 (609bfa5, live-verified: 401s closed, real
   by-date + runs calls OK): GET /briefing/by-date + /runs (hotel token);
   /watchlist GET+POST+DELETE, POST /feedback, GET+PUT /prefs,
   /push subscribe+unsubscribe+prefs, POST /events batch — user-owned
   endpoints authenticate the app's Supabase JWT against GoTrue (5-min
   cache) + hotel_users membership; user_id always from the verified
   token. POST /refresh = existing /trigger.
2. React `api.ts`: point each direct `sb.from(...)` call at the endpoint;
   the read chain already prefers the API when configured. Per-user data
   (watchlist, feedback) carries the Supabase JWT → API verifies it
   against Supabase Auth (JWKS) — auth unchanged, storage moved.
3. Ship app + API together; parallel-run 3 days (API reads PG, Supabase
   dual-write continues). Then stop dual-writes.

### C3 — own login system (2–3 days) — DECIDED 2026-09-10 — CODE SHIPPED 2026-09-30
**Status 2026-09-30:** steps 2, 3 and 5 are built and tested (backend `db/auth.py`,
`db/passwords.py` — stdlib scrypt, not argon2 — `scripts/users.py`; app `lib/session.ts`,
`ChangePassword.tsx`). Both login paths run in parallel; `AUTH=own` closes Supabase.

**Go-live checklist (each is one command, in this order):**
1. Apply the migration AS THE BUILDER ROLE (the web container's fl_app has no
   DDL rights — grants.sql). From the backend folder, Command Prompt or PowerShell:
   ```
   powershell -ExecutionPolicy Bypass -File scripts\pg_migrate.ps1 docs\sql\pg\2026-09-30_own_login.sql
   ```
   (`scripts/pg_migrate.ps1` base64-encodes the file into a `railway ssh -s Postgres`
   psql call that uses the container's own DATABASE_URL — nothing lands on the laptop.)
   Idempotent; ends with the fl_readonly column revokes (no hash columns visible).
2. Deploy: `git push` backend (Railway) and `git push` firstlight-pwa (Pages).
3. Create the first account:
   `railway ssh -s web -- python scripts/users.py create --email <email> --name <Name> --group tor-hotel-group --role owner [--password <pw> --keep-password]`
4. Verify: log in at firstlight.hbis.io as that user → picker shows the three
   Tor hotels + the group entry; bell → `POST /push/test` arrives.
5. Import the Supabase Auth accounts with the SAME uuid:
   `railway ssh -s web -- python scripts/import_supabase_users.py --dry-run` then without
   the flag (passwords printed once, forced change). When everyone has signed in, set
   `AUTH=own` on the web service; remove the Supabase JWT path + `hotel_users` after
   the 1-week parallel-run.
Supersedes the earlier "keep Supabase Auth" recommendation: the user wants
Supabase gone entirely. Schema is in `docs/sql/pg/2026-09-10_tenancy_auth.sql`
(`users`, `sessions`, `memberships`, `hotel_access` view; also in schema.sql).
1. Apply the migration on the Railway instance (ALTERs are idempotent).
2. API: `POST /auth/login` (email + password → opaque session token,
   scrypt (stdlib, `db/passwords.py`), sha256 of the token stored in `sessions`,
   30-day expiry sliding on use; lockout after 10 failures / 15 min),
   `POST /auth/logout`, `POST /auth/change-password`, `GET /me` (user +
   the hotels from `hotel_access`). `auth_user()` verifies the session
   token instead of the Supabase JWT; `require_member()` reads
   `hotel_access` instead of `hotel_users`. Both paths coexist behind an
   env flag (`AUTH=supabase|own`) for the parallel-run.
3. Admin CLI (`scripts/onboard_hotel.py` / `scripts/users.py`): create
   user with a generated initial password (`must_change_password=true`),
   reset password, deactivate. NO email — passwords are read out by
   phone; the app forces a change on first login.
4. Import: every `auth.users` row → `users` with the SAME uuid so
   feedback / watchlist / push_subscriptions / usage_events stay
   joinable; every `hotel_users` row → `memberships (hotel, viewer)`;
   each imported user gets a fresh initial password.
5. App (React): login screen against `/auth/login`, forced
   change-password screen, token in localStorage (same header shape as
   today so `api.ts` changes are minimal). Ship app + API together;
   parallel-run 1 week; then drop the Supabase JWT path and `hotel_users`.

### C4 — tenancy hierarchy (1 day) — DECIDED 2026-09-10
Group → Company (`organizations`, unique VAT per country) → Hotel.
1. Same migration file adds `groups`, `organizations.group_id /
   vat_number / legal_name / country`.
2. Backfill: create the real company rows (VAT from the client files) and
   point each existing hotel's `org_id` at its company; group Pome +
   Potidea only if they share owners (ask).
3. App: hotel picker lists everything `GET /me` returns; an owner sees the
   whole company/group. Merges are admin SQL (ONBOARDING.md §4), never
   deletes.

### C5 — remove the email channel (½ day) — DECIDED 2026-09-10
Delete `briefing/mailer.py`, `templates/email.html`, the SMTP config for
briefings, and the email branch of `notify=`; the morning run sends push
only. KEEP the ops audit/drift email to HBIS (audit.py) — that is
monitoring, not a client channel. `recipient_email`/`recipient_name`
columns stay (deprecated) so nothing breaks; the onboarding intake no
longer asks for them.

## Cutover verification (run after every flip)
- `/health`: prompt_version present, `stale_hotels` empty.
- Trigger manual refresh per hotel → new refresh_runs row in PG, briefing
  updated, app shows it.
- App smoke: login, latest briefing, day strip past day, watchlist add/
  remove, feedback submit, bell toggle, Data health list.
- Nightly backup script now dumps PG too (`scripts/backup_pg.ps1` — write
  during C1 step 4).

## Rollback
- C1: `STORAGE=supabase` env flip (writes never stopped) — minutes.
- C2: ship previous app build (Pages rollback) — Supabase still has data
  from dual-writes — minutes.
- C3: `AUTH=supabase` env flip restores JWT verification (Supabase Auth
  untouched during the parallel-run) — minutes.
- Hard rule: Supabase project stays UNTOUCHED for 14 days after C3
  completes (auth was the last dependency); only then archive.

## Preconditions (gate to start C1)
- ✅ Off-platform nightly backup + verified restore drill (2026-09-04:
  736 rows dumped, reparse-verified; intraday_log restore round-trip OK;
  Task Scheduler "FirstLightBackup" daily 08:30 local).
- ✅ Freshness monitoring live (audit email + /health stale_hotels +
  in-app Data health) — the tripwire that catches migration breakage.
- ⬜ React go-live flipped (Pages primary) and stable ≥ 1 week.
- ⬜ External /health pinger registered (user).
- ⬜ Audit runs clean for 5 consecutive days (first window starts
  2026-09-05 07:10 UTC).
- ⬜ `firstlight_ro` swap done (don't migrate with `sa` in configs).
