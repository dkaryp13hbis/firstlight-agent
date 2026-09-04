# PostgreSQL vs Supabase — what we're actually changing, and why

Decision record, 2026-09-05. Plain-language reference for the Phase C
migration. See [PHASE_C_RUNBOOK.md](PHASE_C_RUNBOOK.md) for execution.

## The one-sentence truth

**Supabase IS PostgreSQL** — a PostgreSQL database wrapped in a hosted
platform (REST API, Auth, dashboard, row-level security). We are not
changing database technology; we are changing **who hosts our Postgres and
how our code reaches it**.

## What each one is

| | Supabase (today) | Railway Postgres (target) |
|---|---|---|
| The database | PostgreSQL, hosted by Supabase | PostgreSQL 18, hosted in OUR Railway project |
| How code reaches it | HTTPS REST calls (PostgREST layer) | Direct SQL over the private network |
| Where it lives | Supabase's cloud, separate from the processor | Same project/network as the FirstLight processor |
| Auth (logins) | Supabase Auth (GoTrue) | **Stays on Supabase** — not migrating |
| Security model | Row-level security (RLS) policies per table | Private-only DB; the app goes through our API with per-hotel tokens |
| Admin UI | Supabase dashboard | pgAdmin via encrypted tunnel |
| Backups | Supabase daily + our nightly off-platform dump | Railway automated + the same nightly dump |

## Why move (the real reasons, in business terms)

1. **Cloud PMS needs it.** Opera Cloud / Hotelizer integration means WE
   store reservations and sync them incrementally. That reservation store
   belongs in a database we fully control, next to the processor.
2. **Speed & cost at scale.** Every refresh today makes dozens of HTTPS
   round-trips to Supabase's REST layer. On Railway's private network the
   same operations are direct SQL — faster runs, no egress, one platform
   bill instead of two as hotel count grows.
3. **Control.** Our own schema, real foreign keys, LISTEN/NOTIFY (refresh
   commands become instant instead of polled every 30s), no tier limits.

## What we deliberately KEEP from Supabase

- **Auth** — logins, password resets, JWTs. Free at our scale, decoupled,
  battle-tested. The API verifies the same JWTs; users notice nothing.
- **The 14-day rollback** — Supabase stays intact and dual-written until
  the migration has proven itself; going back is an env-var flip.

## What changes for each audience

- **GMs / app users:** nothing. Same app, same login, same data — the app
  talks to our API instead of Supabase directly (already built).
- **You (owner):** one dashboard less to think about day-to-day; pgAdmin
  replaces the Supabase table editor; costs consolidate on Railway
  (~$5-10/month for the DB at current size).
- **The code:** writes go through `db/store.py` (live now, dual-write);
  reads flip with `STORAGE=pg` when verification passes.

## The honest trade-offs

- Supabase's dashboard is friendlier than pgAdmin.
- RLS gave the app a direct, safe read path; the API layer replaces it —
  more of our own code to own, but also one consistent access story.
- Railway DB backups + our nightly dump must stay healthy — we own more of
  the safety story ourselves (that's why backups shipped first).
