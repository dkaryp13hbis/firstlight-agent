# FirstLight — Engineering Log

Living document: architecture, migration progress, decisions, incidents, and release history.
Updated with every completed step and every incident. Newest entries first within each section.

---

## 1. What FirstLight is

AI morning briefing for hotels. Pulls live data from the hotel's PMS, computes revenue
signals deterministically, has Claude narrate them into insight cards, and delivers via
PWA (app.hbis.io), email, and push notifications — every morning plus 2 data refreshes.

**Live hotels:** Pome Hotel (Protel, mpehotel 1, 167 rooms) · Potidea Palace (Protel, 236 rooms)

---

## 2. Architecture

### Today (transitional)
- **Hotel servers (Windows, on-prem):** full repo copy runs `server.py --daemon` — an HTTP
  bridge on localhost:8765 exposing `/fetch` (runs SQL against local Protel/BiData) +
  polls Supabase for refresh commands. Exposed via Cloudflare Tunnel
  (`pome-data.hbis.io`, `potidea-data.hbis.io`).
- **Railway:** `railway_main.py` processor (06:00 UTC full briefing with AI + email;
  11:00 & 17:00 UTC data-only refreshes reusing morning AI) + a FastAPI relay
  (`web-production-61c4d.up.railway.app`: `/briefing`, `/my/ai-insights`,
  `/briefing/latest`, `/commands/pending`). Auto-deploys from GitHub
  `dkaryp13hbis/firstlight-agent` main branch.
- **Supabase** (`tqfupsvymisnskiwtjut`): `hotels`, `briefings`, `hotel_users`,
  `push_subscriptions`, `refresh_commands`.
- **Vercel PWA:** app.hbis.io (separate repo `firstlight-pwa`).

### Target (cloud migration, in progress)
Hotel servers run **only** cloudflared (persistent Windows service TCP-forwarding the
PMS DB port). Railway opens on-demand tunnel clients, runs the PMS adapter queries
directly, and holds ALL code, queries, and secrets. Updates = git push. Hotel visits =
onboarding only. See §4 tracker.

### PMS adapter matrix
| PMS | Access | Tunnel | Driver | Status |
|---|---|---|---|---|
| Protel | SQL Server :1433 | yes | pyodbc + msodbcsql18 | **implemented** (`db/adapters/protel_mssql/`) |
| Pylon | SQL Server :1433 | yes | pyodbc | planned |
| Opera 5 | Oracle :1521 | yes | oracledb (thin) | planned |
| Fidelio V8 | Oracle :1521 | yes | oracledb (thin) | planned |
| Hotelizer | cloud REST | no | requests | planned |

All adapters return the same **HotelDataSnapshot** (`db/contract.py`); the analyst,
cards, and app never know which PMS produced the data.

---

## 3. The AI analyst (v4 / cards spec v1.2)

Two layers in `briefing/analyst.py`:

**Layer A — compute (pure Python, no AI):** signals = pickup z-score, pace vs same-time-
last-year, soft/hot dates (90d), month-end projections. Score =
`(0.35·Revenue + 0.25·Urgency + 0.25·Magnitude + 0.15·Novelty) × Confidence`.
Hard gates: pickup |z| ≥ 2; other signals ≥ 10% deviation; ≥ €1,000 at stake.
Projections exposed **only as bands** (occ ±2pts, revenue ±2%) — the point estimate
never reaches the LLM. Facts are period-scoped display strings
(`{"value": "−23.6%", "period": "Aug full month, vs same time last year"}`).
Global ranking (no per-month quota); same-month pace+projection merge; pickup+soft-dates
merge; novelty gate (repeat card within 3 days without ≥10% worsening → watchlist).

**Layer B — narration (one Claude call per card):** the LLM only phrases pre-computed
facts. Validator rejects: any number not verbatim in input; word-cap violations
(headline 12 / what 20 / why 35 / action 25 / by_when 10 words); imperative action
openers (soft suggestions only); sentences blending full-month with remaining-period
numbers. Max 2 retries → deterministic templated fallback card ships instead.
**Narration can never block a briefing** — only data-level failures can.

Output carries both new card anatomy (headline/evidence/what/why/action/by_when/
at_stake+calc) and legacy fields (title/kpis/findings/action) so the current PWA
renders unchanged. Legacy fallback path (`_legacy_generate`) serves old-format payloads.

---

## 4. Cloud migration tracker

### Phase 1 — Build (cloud side)
| Step | Status | Commit | What / why |
|---|---|---|---|
| 1. HotelDataSnapshot contract | ✅ 2026-07-22 | `3bf6605` | `db/contract.py` — canonical payload spec + `data_quality` gate (missing fields, sanity checks, publishable verdict). Bad data now refused loudly; two real incidents replayed as tests (17 checks in `test_contract.py`). |
| 2. Protel adapter | ✅ 2026-07-22 | `4704c37` | Queries + fetch moved to `db/adapters/protel_mssql/`; `fetch_snapshot(conn, hotel_ctx)` takes identity per call; `get_adapter()` registry; old entry points are shims — zero hotel deployment needed. |
| 3. refresh_runs + drop stored HTML | ✅ 2026-07-23 | see below | Operational logbook separate from customer briefings: per-stage timings, data_quality verdict, per-card audit (exact facts given, validation attempts, fallback flag), token usage + estimated USD cost per briefing (sonnet-4-6 rates incl. cache). Status success/degraded/failed; degraded = fallback cards shipped. Railway no longer stores rendered_html — JSON is canonical, email renders transiently, `briefings.source_run_id` links to the run. RunLogger is fail-open (logging can never break a briefing). Requires one-time SQL: `docs/sql/2026-07-23_refresh_runs.sql` in Supabase. |
| 4. Tunnel Connection Manager + Dockerfile | ✅ 2026-07-23 | see below | `db/tunnel.py`: on-demand Railway-side cloudflared Access clients — port pool 14330-99, global cap (TUNNEL_CONCURRENCY=5), per-hotel single-flight, readiness health-check, guaranteed cleanup + atexit sweep, service tokens via env only (12 tests, real subprocess/socket). Dockerfile replaces nixpacks: python 3.12 + msodbcsql18 + cloudflared. `connect_mssql()` for explicit-address connections (Encrypt=no — tunnel already encrypts). Railway fetch switch: `pms_config.fetch_mode: "tunnel"` → adapter via tunnel, ANY failure → automatic bridge fallback, path + error logged to refresh_runs. `_get_hotels` falls back to legacy columns if migration SQL not yet run. Requires SQL: `docs/sql/2026-07-23_step4_tunnel.sql`. Hotel-side cloudflared stays permanently running (Windows service). |
| 5. Pipeline hardening | ✅ 2026-07-23 | see below | Per-hotel single-flight lock (concurrent refresh skipped); staged timeouts (warn 180s, hard-abandon 480s, env-tunable) via worker thread + first-finish-wins on RunLogger; retry ladder 5/15/45min for failed runs (dedup: retry skips if a briefing appeared meanwhile; no_morning_ai not retried); knobs REFRESH_CONCURRENCY (default 1 until config globals de-globalized — open item), CLAUDE_CONCURRENCY (semaphore in analyst), TUNNEL_CONCURRENCY; word-cap prompt tune (~80% targets, prompt cards-v1.2.1) to cut validation retries; `refresh_runs.attempt` column (schema-tolerant writes). 9 tests. SQL: `docs/sql/2026-07-23_step5_attempt.sql`. |

### Phase 2 — Pome pilot
6. ✅ 2026-07-23: Cloudflare TCP route `sql-pome.hbis.io → tcp://192.168.100.7:1433` on
   existing FL_pome tunnel + Access app with Service Auth policy + service token
   `railway-pome-sql` (per-hotel; in hotels.pms_config). Edge verified: no token → 403,
   token → 200. NO hotel-server visit was needed (route added remotely to running tunnel).
7. ✅ 2026-07-23 21:15 UTC — **FIRST TUNNEL-DIRECT BRIEFING**: fetch_path="tunnel",
   fetch 7.9s (vs ~1-2s bridge; includes client spawn + Access handshake + 11 queries),
   zero tunnel errors, no fallback. Railway queried Pome's SQL directly; no FirstLight
   code involved at the hotel. Bridge stays armed as fallback.
7b. ✅ 2026-07-24: **Pome server decommissioned to tunnel-only.** Cloud command
   poller (`_poll_refresh_commands`, atomic claim) + 03:30 UTC full-run schedule
   replace the daemon and Task Scheduler triggers (`8e1329e`). Daemon killed,
   both FirstLight tasks disabled; refresh-button test passed with zero hotel-side
   code (21:32 run: poller-claimed, fetch_path=tunnel). Folder stays 1 week as
   rollback. Also `5402b37`: manual refreshes now silent (no email/push) — a
   debugging day had sent the GM 6 briefing emails; notifications only from
   scheduled runs now.
8. ⬜ Pilot week: watch refresh_runs (tunnel reliability, fallback count).
   Conditions before Potidea/Phase 3: (a) create read-only SQL login `firstlight_ro`
   on Pome's SQL Server and swap it into pms_config (currently sa — flagged);
   (b) word-cap compliance: v1.2.1 tune insufficient — violations now near-misses
   (cap+1..9 words); next: targeted retry feedback quoting offending field + budget,
   or relax caps by ~3 words (product decision).

### Phase 3 — Complete
9. ⬜ Potidea tunnel setup · 10. ⬜ **Delete code folders from both hotel servers** ·
11. ⬜ Cost/latency review from audit data (per-card vs consolidated Claude calls decision)

### Phase 4 — Scale readiness (before hotel #10)
12. ⬜ Load test 20–30 simulated hotels · 13. ⬜ Concurrency tuning (REFRESH=10/TUNNEL=5/CLAUDE=8) ·
14. ⬜ Per-hotel briefing time + timezone

---

## 5. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-07-22 | Tunnel-direct architecture: hotel servers keep ONLY cloudflared | Protect product IP (code/queries/keys off customer hardware); updates become git-push-only |
| 2026-07-22 | NO watermarks / incremental ETL | We run stateless bounded aggregate queries, not a warehouse copy — watermarks add state-sync bugs for no gain |
| 2026-07-22 | Keep per-card Claude calls (vs consolidating to 1–2) | Validator isolation: one bad card retries/falls back alone. Revisit only if measured cost/latency says so (Step 11) |
| 2026-07-22 | `refresh_runs` (ops) separate from `briefings` (business) | 3 failed attempts + 1 success = 1 customer briefing, 4 audit rows; failed runs must never mix into customer-facing data |
| 2026-07-22 | Publication rule: valid data snapshot → publish; narration failures degrade (fallback cards), never block | GMs always get a briefing; `degraded` flag tracks fallback frequency |
| 2026-07-22 | HTML rendered on demand, JSON is canonical | Data is 10KB/briefing; stored HTML was the only storage-scale problem |
| 2026-07-22 | Stay on Railway (EU region), revisit at ~20+ hotels | Migration cost > savings at current scale; everything is Docker-portable |
| 2026-07-22 | Claude cost €1–3/hotel/mo treated as UNPROVEN until measured | Estimate only; refresh_runs token logging produces the real number |
| 2026-07-22 | Potidea interim file-update SKIPPED | Legacy briefings work fine (guard fix); avoid repeating the manual process we're eliminating; both hotels get final architecture in Phase 3 |
| 2026-07-21 | v1.2 spec before migration Step 1 | Customer-visible quality first; also settles the fact shapes the contract then codifies |
| 2026-07-20 | Two-layer analyst: all math in Python, Claude narrates only | Kills hallucinated numbers structurally; validator enforces verbatim copying |

---

## 6. Incident log ("bad days")

| Date | Incident | Root cause | Fix / lesson |
|---|---|---|---|
| 2026-07-23 | App showed "No briefing available" for both hotels after Step 3 deploy, despite push notifications arriving | Step 3 stopped storing `rendered_html`; the PWA renders briefings FROM that field (assumption that it renders from `data` was wrong — the API endpoint was checked, the app's own reads were not) | Hotfix `297ce90` restored HTML storage; both hotels republished within the hour. Lesson: before removing a field, verify what every consumer actually reads — not just the API in front of it. PWA render-from-data is now a prerequisite (open item) before storage removal returns. Side note: dropping the blob had instantly fixed Potidea's `/briefing/latest` 500 — that endpoint chokes on large rows |
| 2026-07-23 | Pome produced no morning briefing (3 failed runs: 06:30, 09:00, 14:00 Greece) | `server.py --daemon` was dead — restarted manually in an RDP window the day before, killed when the session was signed out; startup task only fires on reboot. Bridge returned 502 (tunnel up, origin down) | Daemon restarted; refresh republished. Diagnosed in one refresh_runs query (logbook's first save). Lessons: console-started processes die on sign-out — use Task Scheduler / disconnect only; the publication gate correctly kept the last good briefing; this failure class disappears entirely with tunnel-direct (no daemon) |
| 2026-07-22 | Both hotels shipped a **1-insight briefing** | Old-format payloads (hotel daemons not restarted / never updated) let only the future-projection signal fire; non-empty ranked list skipped the legacy fallback | Guard on new-data presence (`b6d4185`), later formalized as `data_quality.legacy_mode` (Step 1). Lesson: fallback conditions must test *inputs*, not *outputs*; running daemons cache old code — restart after file updates |
| 2026-07-21 | 30 min lost hunting Railway `/trigger` 404 | Two Railway services: FastAPI relay (`web-production-61c4d`) ≠ processor (`railway_main.py`); we probed the relay | Documented both services (§2). Lesson: check prior session history first — the answer usually exists |
| 2026-07-21 | Real-data cards review found: full-month rn presented as remaining-period ("4,161 rn in 10 nights"), weak card fired (z +0.7), unexplained €108,298 at stake | No period labels on facts; magnitude gate not enforced; at-stake figure without calc | Spec v1.2 (`3f6063a`): period-scoped facts + blend validator; |z|≥2 gate; no-calc-no-figure rule |
| ~2026-07-12 | Potidea showed **zero-data briefings** twice | Empty payload saved over good briefing; only guard was ad-hoc rev==0 check added after the fact | Now a hard contract sanity check (`yesterday_nonzero`) blocking publication (Step 1, tested) |
| ongoing | Dev machine cannot call Anthropic API (SSL "Connection error") | Local Python SSL cert issue; curl works | Narration tested in production (Railway); local tests cover compute + validators; fallback path proved resilient (all cards shipped) |
| open | FastAPI `/briefing/latest` returns 500 for Potidea's hotel_id (Pome works) | Unknown — possibly row size or null field | To investigate; PWA unaffected (reads Supabase directly) |
| historical | Pome server folder is a ZIP download (`firstlight-agent-main`), no git installed | Onboarding shortcut | Made updates painful (curl per file + daemon restart) — a driver of the cloud migration |

---

## 7. Release history (backend repo, newest first)

| Commit | Date | What |
|---|---|---|
| `4704c37` | 2026-07-22 | Step 2: Protel adapter behind PMS drawer; back-compat shims |
| `3bf6605` | 2026-07-22 | Step 1: HotelDataSnapshot contract + data_quality publication gate |
| `3f6063a` | 2026-07-22 | Analyst v1.2: soft language, global ranking, projection bands, period-scoped facts, hard gates, novelty gate |
| `b6d4185` | 2026-07-22 | Guard v3 analyst path behind presence of new payload fields (1-insight fix) |
| `c5e5eae` | 2026-07-21 | Narration layer per cards spec v1.1: per-card calls, numeric validator, fallback cards, at-stake calcs |
| `79a4655` | 2026-07-21 | Two-layer analyst v2 + 3 new SQL queries (Q9 pickup daily, Q10 OTB-by-date-90, Q11 current month remaining) |
| `3d4f883` | 2026-07-12 | 14:00 + 20:00 data-only refreshes; zero-data guard |
| `a6376b0` | 2026-06-26 | Chart UX improvements; briefing dedup |
| `3c09074` | 2026-06-18 | Analyst math consistency + booking window context |
| `4f7d8c9` | 2026-06-18 | Insight cards redesign: findings + action + metric sub line |

---

## 8. Open items (not scheduled)

- PWA update to render the new card anatomy (BY WHEN box, tappable AT STAKE calc,
  evidence labels) — backend already ships the fields
- PWA: render briefings from `data`/`ai_insights` JSON instead of `rendered_html` —
  PREREQUISITE for removing HTML storage (see 2026-07-23 incident); also permanently
  fixes the large-row 500 on `/briefing/latest`
- Potidea `/briefing/latest` 500 (see incidents)
- Signal 3 (lead-time/booking-window signal) — deferred
- Follow-up loop (track advice given → outcomes; N component of score) — Phase 2,
  builds on refresh_runs card audit
- Chatbot agent on briefing data; monetization tiers — discussed, parked
