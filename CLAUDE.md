# FirstLight — backend (hotel morning briefing)

Cloud processor that fetches PMS data from hotels through Cloudflare tunnels,
computes revenue signals, narrates AI insight cards + a hero paragraph with
Claude, renders/publishes the briefing to Supabase, and notifies the GM.
Hotel servers run ONLY cloudflared — all code, queries, and keys live here.

## Golden rules

- **Deploy = `git push` to `main`** → GitHub → Railway auto-builds the
  Dockerfile. There is no other deploy path. Never hotfix on a server.
- **Never break a briefing.** New queries/signals must be fail-open (see
  `lead_time` in the fetcher: try/except → empty list, publication proceeds).
  Narration failures degrade to fallback cards — only data-level failures block.
- **Claude runs once per hotel per day** (03:30 UTC scheduled run). Manual and
  data-only runs REUSE the day's `ai_insights` and are silent (no email/push).
- **Update [docs/ENGINEERING_LOG.md](docs/ENGINEERING_LOG.md) after every
  step, incident, or decision** — release history, decision log, TO-DO list.
  This is a user-mandated living document.
- Supabase schema changes = SQL file in `docs/sql/` + the user pastes it in the
  Supabase SQL editor (no migration tooling). Code must tolerate the column
  not existing yet (schema-tolerant writes, see `briefing/run_log.py`).

## Dev environment quirks (Windows)

- Local Python 3.11 is broken — run tests with `py -3.13`, and set
  `PYTHONIOENCODING=utf-8` (test output contains — and €).
- Tests are plain scripts, no pytest: `py -3.13 test_contract.py`,
  `test_leadtime.py`, `test_hero.py`, `test_tunnel.py`.
- PowerShell is 5.1: no `&&`; never put double quotes inside `git commit -m`
  here-strings (5.1 mangles embedded quotes passed to native exes).
- `curl`/`curl.exe` fail with SSL error to Supabase — use `Invoke-RestMethod`.
  Local Python cannot reach the Anthropic API (SSL) — narration is verified in
  production via `refresh_runs.cards_audit`, compute layers are tested locally.
- `gh` CLI is not installed. Verify deploys by behavior: trigger a manual
  refresh (insert into `refresh_commands`) and read the new `refresh_runs` row.

## Map

| Path | What |
|---|---|
| `railway_main.py` | Scheduler (03:30 full, 06:00 catch-up, 11:00/17:00 data-only UTC), refresh_commands poller (30s), per-hotel pipeline: fetch → gate → AI → render → publish → notify. Stdlib `http.server`, not FastAPI. |
| `db/contract.py` | HotelDataSnapshot contract + `data_quality` + `is_publishable` gate. Signal fields optional via `_SIGNAL_PREFIXES`. |
| `db/adapters/` | One folder per PMS (`get_adapter(pms_type)`). `protel_mssql/queries.py` = Q1–Q13, `fetcher.py` = snapshot builder. |
| `db/tunnel.py` | On-demand `cloudflared access tcp` clients (port pool, caps). |
| `briefing/analyst.py` | Two layers: deterministic compute (signals 1–5, gates, scoring, merges, novelty, hero slots) + Claude narration (per card + hero, validators, fallbacks). |
| `briefing/run_log.py` | Fail-open RunLogger → `refresh_runs`. |
| `briefing/cloud_push.py` | Publish to Supabase (`notify=` controls push). |
| `templates/` | Jinja2 report + email HTML. |
| `docs/ENGINEERING_LOG.md` | Architecture, migration tracker, decisions, incidents, releases, TO-DO. Read this first for context. |

## Skills

Task-specific knowledge lives in `.claude/skills/` — load the matching skill
before working in that area: `pms-queries` (write/change PMS SQL),
`analyst-signals` (add/tune signals, cards, hero), `ops-monitoring`
(check runs, incidents, Supabase access patterns), `hotel-onboarding`
(add a hotel / migrate one to the tunnel).
