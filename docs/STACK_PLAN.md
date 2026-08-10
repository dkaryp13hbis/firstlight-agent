# FirstLight — Infrastructure & Stack Plan (summary)

Written 2026-08-04, consolidating the decisions of 2026-07-26/27 (full
rationale in ENGINEERING_LOG §5). This is the reference for "what are we
migrating to and in what order". Independent of the pilot — nothing here
blocks daily operation.

## Where we are today

| Layer | Today | Notes |
|---|---|---|
| Hotel side | cloudflared tunnel only (Pome; Potidea still legacy bridge) | end-state reached for Pome |
| Processor | `railway_main.py` on Railway — stdlib http.server + polling loop | scheduler 03:30/06:00/11:00/17:00 UTC + refresh_commands poller (30s) |
| Relay | small FastAPI service on Railway (`/trigger`, `/briefing/latest`) | legacy; partially broken for Potidea |
| Data | Supabase (Postgres + PostgREST + auth) | briefings, refresh_runs, hotels, hotel_users, insight_feedback, hotel_prefs |
| Frontend | single-file PWA on Vercel, iframe over `rendered_html`, reads Supabase directly | anon key + RLS |
| AI | Claude once/hotel/day, single-shot narration, deterministic fallbacks | ~$0.04–0.05/hotel/day |

## Target stack (agreed)

**Cloudflare** (network + Pages) → **one FastAPI container on Railway**
(API + scheduler + processor) → **Railway managed Postgres**. React frontend
on Cloudflare Pages rendering **from data** (no more rendered_html). FastAPI
becomes the ONLY gateway — the frontend never touches the DB directly.

## The three phases (sequenced, each shippable + rollback-able)

### Phase A — FastAPI consolidation (~3–4 days, invisible to users)
- Rebuild the processor as a FastAPI app: uvicorn **1 worker**, APScheduler
  in the lifespan hook (1 worker rule: N workers = N schedulers = duplicate
  briefings/emails).
- Port `/trigger` + `/briefing/latest`; retire the broken relay service.
- **Per-hotel API tokens** (auth foundation for Phase B).
- `kpi_summary` column + `GET /briefing/history` (7-day history feature).
- `POST /feedback` reading the 👍/👎 loop.

### Phase B — React on Cloudflare Pages (~1–2 weeks)
- Audit the PWA repo first; then rebuild as React components rendering
  **from data** via FastAPI tokens (drop `rendered_html` storage).
- Carries the accumulated UI wish-list: new card anatomy, hero block,
  Greek/English UI strings (~150), text-size 1–5, bigger OTB charts,
  7-day history UI, chart components per the design-handoff tokens
  (44px rows, no chart library).
- Parallel-run against Vercel, then retire Vercel.

### Phase C — Postgres on Railway (~2–3 days + 2-week rollback window)
- **Railway managed Postgres** (NOT Postgres-in-container — ephemeral FS).
- `db/client.py` consolidation → `pg_dump`/restore → env-var flip.
- Queue = `refresh_commands` with `FOR UPDATE SKIP LOCKED` + LISTEN/NOTIFY
  (replaces the 30s poll).
- Verify backups + nightly dump; retire Supabase.
- Migrates together with B as ONE coordinated release window — once the
  frontend reads via our API, PostgREST value evaporates.

### Phase 4 — scale prep (before hotel #10)
- De-globalize `config` module → `REFRESH_CONCURRENCY=10` (fleet morning:
  ~15 min / 100 hotels), load-test 20–30 hotels.
- Per-hotel briefing time + timezone (per-hotel APScheduler crons, tz-aware).
- Web/worker split when needed: same image, two Railway services.

## Standing decisions (don't relitigate)

- **No Celery.** ~1k tasks/day at 200 hotels doesn't justify a Redis broker.
  Postgres-native queue; **Procrastinate** if task ergonomics wanted.
  Revisit only at >50k tasks/day or multi-machine workers.
- **No separate backend/frontend hosting complexity beyond the target**:
  Cloudflare + one Railway container + Railway Postgres. (DigitalOcean,
  Cloudflare Workers-as-backend, docker-compose stacks: evaluated, declined.)
- **Concurrency model**: reads scale via precomputed briefings; duplicate
  refresh presses collapse to one run (atomic claim + per-hotel lock + AI
  reuse); the only real fleet problem is the 03:30 burst → Phase 4.
- **Claude once/hotel/day, single-shot, word-cap contract** — cost policy is
  orthogonal to the stack change; React changes none of the caps.
- **Deploy = git push** stays the only deploy path throughout.

## Product roadmap riding on the phases

- Feedback learning loop (Tier 1 ranking-weight tuning; guardrail: never
  suppresses hard-gated facts) — API in A, UI in B.
- 7-day history — column + endpoint in A, UI in B.
- Onboarding kit v1 → onboarding agent v2 (Cloudflare API provisioning,
  SQL discovery probe, LLM diagnostic loop).
- `db/adapters/SEMANTICS.md` before PMS adapter #2.
- Real TTS (MP3 at morning run, ~$0.01/day) in Phase A if device voices
  disappoint; revenue bridge; chatbot later.
