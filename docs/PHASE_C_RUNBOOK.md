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
| (auth.users) | — | Supabase Auth | login | C3 (likely never) |

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

### C3 — auth (decision, not necessarily work)
RECOMMENDATION: keep Supabase Auth permanently (free at this scale,
decoupled, battle-tested). Revisit only if Supabase is fully retired.

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
- Hard rule: Supabase project stays UNTOUCHED for 14 days after C2
  completes; only then archive.

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
