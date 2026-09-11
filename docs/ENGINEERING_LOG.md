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
Data + identity (decided 2026-09-10): our own Postgres on Railway with our own
login system (no Supabase at all); tenancy Group → Company (unique VAT) → Hotel,
users attached at any level; public app https://firstlight.hbis.io; push + app
only, no email channel.

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
facts. Validator rejects: any number not verbatim in input; word-cap violations;
imperative action openers (soft suggestions only); sentences blending full-month
with remaining-period numbers. **Plain-language contract (2026-08-24, `cards-v1.9-plain`):**
the reader is a hotel owner, not a revenue manager — short sentences, everyday words,
no jargon when a simpler phrase means the same (pickup → new bookings, pace → bookings
vs last year, OTB → booked so far, ADR → average rate, close-in → last-minute,
compression → dates filling up fast, soft dates → low-booking dates, firming/softening →
getting stronger/weaker). Enforced on every path: prompt glossary (cards + hero) →
`_plainify_text()` deterministic substitution pass on narrated output AND fallback
templates (English only; logs + `jargon_replaced` in cards_audit when it fires — a hit
means the prompt let something through) → all fallback templates rewritten in plain
words → hero driver hints rewritten ("mostly from higher rates", "fewer rooms sold"). SINGLE attempt (cost policy 2026-07-27,
`NARRATION_ATTEMPTS` env to re-enable retries) → deterministic templated fallback
card ships instead. **Narration can never block a briefing** — only data-level
failures can.

**Word-limit contract (canonical, `_WORD_CAPS` + `_HERO_WORD_CAP` in analyst.py):**

| Field | Cap | First-attempt target (~80%) |
|---|---|---|
| headline | 12 | ≤ 9 |
| what_happened | 20 | ≤ 16 |
| why_it_matters | 35 | ≤ 28 |
| recommended_action | 25 | ≤ 20 |
| by_when | 10 | ≤ 8 |
| hero paragraph | 110 | ≤ 90 |

Enforced on EVERY path — nothing the briefing ships may exceed a cap:
1. tool-schema field descriptions state the cap + target at generation time;
2. validators reject over-cap narration (single shot → fallback);
3. fallback templates are test-proven within caps (test_leadtime/test_hero —
   any NEW fallback template must add the same check);
4. `_enforce_caps()` / `_clamp_words()` runtime clamp as last resort (logs a
   CAP CLAMP warning = template bug to fix).

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

### TO-DO list (updated 2026-07-27 — read this first)

**PLATFORM DECISIONS 2026-09-10 (user) → new Phase C work, in order:**
- ⬜ **C3 own login system** (2–3 days): apply `docs/sql/pg/2026-09-10_tenancy_auth.sql`
  on Railway PG → API `/auth/login|logout|change-password`, `GET /me`,
  `auth_user()` on session tokens, `require_member()` on `hotel_access`,
  `AUTH=supabase|own` flag → admin CLI for users/passwords (no email;
  phone handover) → import auth.users SAME uuid + hotel_users →
  memberships → React login + forced change-password → 1-week parallel
  run → drop JWT path + hotel_users. Details: PHASE_C_RUNBOOK §C3.
- ⬜ **C4 tenancy** (1 day): backfill real companies (VAT from client
  files), `org_id` per hotel, group Pome+Potidea only if same owners
  (ASK); picker from `GET /me`. PHASE_C_RUNBOOK §C4.
- ⬜ **C5 remove email channel** (½ day): delete mailer.py + email.html +
  briefing SMTP + the email branch of `notify=`; push only. Keep the ops
  audit email. PHASE_C_RUNBOOK §C5.
- ⬜ `scripts/onboard_hotel.py` — intake sheet → company/group/hotel/users
  in one transaction + printed handover sheet (ONBOARDING.md §2).
- ⬜ Ask the user: Pome + Potidea — same owners (one group) or not? VAT +
  legal name for each.

**Done this week:**
- ✅ 2026-07-24: first fully-cloud scheduled briefing VERIFIED (Pome via tunnel,
  one email + one push; Potidea's stray 04:00 /trigger handled silently)
- ✅ 2026-07-25: Signal 3 (booking lead time) shipped (`be83592`); verified live
  2026-07-26 — first card `leadtime_jul_2026`, 1 attempt, 0 problems
- ✅ 2026-07-26: Hero paragraph shipped (`c4b3e46`); CLAUDE.md + 4 routed skills
  (`2ed8199`); target stack + no-Celery + scheduling decisions logged
- ✅ 2026-07-27: Hero LIVE debut — 1 attempt, 6.1s, 0 validation problems;
  occupancy-vs-rate driver narrated correctly ("rate-for-volume trade")

**NEXT SESSION PLAN (prepared 2026-08-18 evening, for 2026-08-19):**
1. Morning check (5 min): 03:30 run + 04:15 demo sync → both hotels + both demo
   hotels carry `revenueNet`; toggle Net on demo account and eyeball totals.
2. ✅ Reporting-year selection → moved into Settings (done morning of 08-19).
3. Net follow-up (backend, ~1h, live tunnel validation): logisnet into pickup
   windows/daily, top channels, Q16, bridge sources → scaling estimate removed.
4. Go-live prep for React: real push subscription on the bell (then run the
   notification-rules test on a real iPhone + Android with the 3-type content),
   final data-parity pass Vercel vs Pages, then flip DNS/link → Vercel kept 2 wk.
5. If time: share-as-image per section (highest-asked UX gap), then i18n layer.
User-side unblockers still open: firstlight_ro logins (both SQL servers),
Potidea old-daemon decommission, Protel real-rooms + season-dates queries.

**REACT APP PUNCH-LIST (pinned by user 2026-08-16, pre-go-live):**
- ✅ 2026-09-04 **REACT GO-LIVE — https://firstlight.hbis.io** (user decision
  after domain exploration: firstlight.io/.co/.eu taken — RDAP false
  positives corrected by NS lookups; subdomain of hbis.io chosen
  deliberately, "an HBIS app" brand architecture; firstlight.travel noted
  as the spin-off insurance option, not bought). Custom domain on the
  Pages project (same CF account as the zone → auto DNS+cert). VERIFIED
  end to end: current bundle, sw.js push handler, standalone manifest
  #061535, icons, Data health + tracking markers present. Backend 9ca3bce:
  PWA_URL default → firstlight.hbis.io (notification taps). TERMS:
  firstlight.hbis.io = the official address for GMs/testers from today;
  firstlight-pwa.pages.dev keeps working as alias (existing installs +
  push subscriptions unaffected — origin-bound, untouched); legacy Vercel
  app untouched as ROLLBACK STANDBY until 2026-09-18; first morning cycle
  on new domain = 2026-09-05 03:30 (also clean-audit day 1 of 5 for the
  Phase C gate). Tester tracking first snapshot at go-live: 18 events /
  5 sessions (iOS standalone + browser + desktop), tabs→AI Insights,
  card_expand leadtime_sep, history_view used. ⬜ user: Railway PWA_URL
  env check; external /health pinger still open.
- ✅ 2026-09-04 **BACKUPS + PHASE C RUNBOOK** (user: "I want to do everything
  now — start with your suggested steps"; also declared Claude = CTO,
  best-practice-by-default standing rule, saved to memory).
  (a) Off-platform nightly backup: `scripts/backup_supabase.ps1` (PowerShell
  transport — local Python can't SSL to Supabase) dumps all 12 tables as
  NDJSON.gz to C:\FirstLightBackups\YYYY-MM-DD\ (creds in backup.env
  there, NOT in repo), reparse-verifies, prunes >14 days; first dump 736
  rows / ~4.2MB verified; Task Scheduler "FirstLightBackup" daily 08:30
  local. RESTORE DRILL PASSED: intraday_log round-trip via on_conflict
  ignore-duplicates (idempotent). `scripts/backup_supabase.py` = same for
  Linux/Railway later (has --restore <folder> --table <name>).
  (b) [docs/PHASE_C_RUNBOOK.md](PHASE_C_RUNBOOK.md): full 12-table
  inventory with app-direct-access map, C1 backend storage w/ dual-write
  window → C2 app-via-API (8 new endpoints, JWT verified against Supabase
  Auth) → C3 keep Supabase Auth (recommendation); cutover verification
  list; rollback = env flip / Pages rollback, Supabase untouched 14 days.
  GATE to start C1: React go-live stable 1wk + external pinger + 5 clean
  audit days + firstlight_ro swap. Noted in passing: usage_events has 18
  rows (tester tracking LIVE), intraday_log 2 rows (intraday pushes FIRED).
  refresh_runs RLS verified end-to-end as demo user (sees only own hotels;
  demo hotels legitimately have no runs — real accounts see history).
- 🔄 2026-09-04 **DATA-FRESHNESS MONITORING** (user: servers sometimes error
  and nobody notices stale data; wants Power-BI-style refresh history in the
  app). Backend 765af25 + React d6d023e + SQL
  2026-09-04_refresh_runs_read.sql (USER MUST PASTE for the in-app history).
  Four layers: (1) `briefing/audit.py` daily 07:10 UTC — per hotel MISSED
  (report_date < yesterday) / FAILED (runs today, none ok) / FROZEN (zero
  bookings+cancels 2+ days in open season, or identical 'yesterday' across
  report days = frozen PMS export; skipped when MTD=0 closed season) → ONE
  ops email to OPS_EMAIL (default dk@) only on problems; offline smoke test
  passed (fresh silent, stale MISSED+FAILED, frozen 2 flags). (2) /health
  gains `stale_hotels` (60s cache) → ⬜ USER: register a free external
  pinger (UptimeRobot-style) on /health alerting on unreachable OR
  non-empty stale_hotels — covers Railway-down. (3) App: amber banner on
  Today when briefing older than yesterday → opens Data health. (4)
  Settings → **Data health** sheet: freshness verdict, frozen-data warning
  (movement over last 2 report days), refresh history last 3 days grouped
  by day (time/type/retry/error_type/duration/status badges incl.
  degraded); reads refresh_runs via new member-scoped RLS select policy
  (service-role writes untouched); `data_health_open` tracked.
- 🔄 2026-08-24 **MY WATCHLIST v1 — BUILT** (React `28a249c`, backend
  `8df3c65`; user: "lets do this"; spec [docs/WATCHLIST_SPEC.md](WATCHLIST_SPEC.md);
  SQL `docs/sql/2026-08-24_watchlist.sql` — USER MUST PASTE; PWA not yet
  pushed to Pages). GM pins a
  stay month or a date range; one deterministic card per item on Today
  (after the MTD strip), rebuilt on every refresh, zero Claude, plain
  words, no derived euros. Decisions taken: per-USER rows (own-rows RLS),
  placement after MTD strip, cap 5. Gate `WATCHLIST_EMAILS` in
  `lib/watch.ts` was demo-only for the first build, then OPENED to everyone
  (null) the same evening at the user's request ("apply to Pome and
  Potidea") — per-user rows, so each GM/RM keeps their own list.
  PWA files: `lib/watch.ts` (pure compute: month/range lines, status
  ladder NEW→IMPROVING/WORSENING/STEADY, PASSED, CLOSED, PENDING; sheet
  helpers), `lib/speed.ts` (booking-speed math extracted from
  `BookingSpeed` so chart + watchlist can't disagree), `api.ts`
  (fetchWatchlist → null when table missing = section hidden; addWatch
  maps 23505/relation errors to toasts; removeWatch; fetchPrevBriefing =
  previous report_date row for "since yesterday"; demo hotel →
  localStorage), `components/Watchlist.tsx` (section + cards + WatchSheet:
  Month chips with current gap / Date range with From–To + presets This
  weekend · Next 7 · Next 14 · ⚠ soft runs from the heatmap rule ·
  "Tomorrow you'll see" preview · "n of 5 used"), 👁 toggle on OTB month
  headers (`Overview.tsx`), Watch/Watching pill on month-scoped AI cards
  (`AiCards.tsx`, month parsed from card id), `types.ts` now types
  pickup_daily/cancel_daily/otb_by_date, Info ⓘ key `watch`. Tracking:
  watch_add {kind,key,from: sheet|otb_card|ai_card}, watch_remove,
  watch_tap (→ Pace tab). Hidden in past-day view; reset on hotel switch.
  Verified: `tsc -b` clean, `vite build` OK, fixture checks (scratch
  script, 8 cases) all pass, headless Edge render of a `--mode demo` build
  shows the section in place (fixture mode seeds two sample watches so
  the dev preview is never empty). ALSO FIXED on the way (trust bug the
  product review flagged): Smart Summary "Last refresh NaN undefined" —
  `SmartSummary.tsx` parsed `data.report_date` (a display string) instead
  of `briefing.report_date` (ISO); now "09 AUG · 14:45". HEATMAP ENTRY added same evening
  (user: "build the heatmap"): cells in Next 60 Days Demand are tap
  targets — one tap = a date, second tap = a range (blue ring + scale on
  selected cells); panel under the grid shows booked % vs LY for the
  selection and offers Watch <date> / Watch this week (Mon–Sun, clamped
  to the first date in view) / Watch <soft run> · behind LY (amber, only
  when the date sits in a flagged run of ≥2 dates) / the picked range;
  already-watched → "Watching ✓" disabled; tracked as watch_add
  {from: heatmap}. `softRuns/rangeKey/rangeTitle/isoAdd` reused from
  lib/watch.ts; `OtbTab` threads `onWatchRange/watchedRanges`. TREND STRIP added 2026-08-25 (user:
  "any chart to check the performance?" → "lets build 1 for now" = no
  backend change): tap a watch card → expands to a sparkline of rooms
  booked (month) / booked % (range) THIS YEAR (blue, area) vs LAST YEAR
  same point (grey dashed), endpoints labelled, one point per stored
  morning briefing (last 7 report dates via `fetchHistoryRows` — the
  same rows the day strip opens, fetched lazily once per hotel on first
  expand); a row of per-day status glyphs (▲ ▼ — ✓) computed exactly as
  the card read each morning (`watchStatusHistory`); for months, net
  rooms per booking day bars from today's `pickup_daily` (no history
  needed). <2 points → "builds up day by day". Net-rooms bars were added for ranges too (from morning-to-morning deltas, `netRoomsFromSeries` kept in lib) and then REMOVED entirely the same day (user: "does not look good") — strip = sparkline + status glyphs only. Last point is labelled "Today" (report_date = the day the briefing reports on; the day strip uses the same convention — user asked why it showed the 24th on the 25th). "Open in Pace ›" link
  replaces the header tap-through. Tracked `watch_expand`. Option 2
  (widen `kpi_summary` for slim 30-day history) stays open in spec §7.
  Past-day view: watchlist deliberately hidden (v1); proposed as-of-day
  replay with created_at filter — user has not decided. NOT DONE:
  Note editing (column exists), Greek strings (no dictionary yet), source
  watches, push line, novelty-gate awareness (all listed in spec §7).
  ⬜ NEXT: paste SQL → open demo account → add October + a range → next
  morning check the pill flips from "First day watching"; then widen the
  gate. Context: came out of the product-evaluation review (commercial
  memory > more BI); sibling item = story status enum on cards — not yet
  planned.
  Mock (2026-08-24): artifact 93a332c0-aa88-49a9-a924-4b4226709913 — Today
  entry, add sheet (Month | Date range, "tomorrow you'll see" preview, 5-cap
  counter), 7-day evolution of the October watch with status pills
  (First day watching → Getting worse → Steady → Improving → Closed) + gap
  sparkline; range watch shown alongside.
- ⬜ **WEEKLY DIGEST** (user: "I like it a lot" — parked 2026-08-24, do NOT
  build yet). Spec agreed: Monday after 03:30 run; six deterministic blocks —
  one-line verdict, week scorecard vs same days LY, MTD + FY OTB, pickup
  (booked/cancelled/net, best/worst day), "what the analyst flagged" (still
  open/new/cleared from follow-up memory), next-weeks outlook + callout; no
  Claude by default; Greek/English via dictionary; delivery = in-app entry
  in the day strip + push (4th notification type "Weekly digest") +
  share-as-image for WhatsApp. ACCESS (agreed 2026-08-24): a "Last week"
  pill at the left of the day strip, always = last completed Mon–Sun (any
  weekday), scrolling left to "2/3/4 weeks ago"; closed months appear as
  month pills ("July") in the same strip; Monday push deep-links to the pill;
  flags block computed AS OF that week's end (honest history); optional
  "This week so far" only if testers ask. Mocks: content artifact c8fb2f2c…, design
  canvas e59bb657… (3 chart styles: A Scorecard tiles+bullet bars, B
  Storyline dumbbells, C Chart-first cumulative curve+rings) — STYLE NOT
  CHOSEN YET; ask before building.
- ⬜ **MONTHLY DIGEST** (parked with the weekly): fires when a month closes
  (1st, after 03:30); same skeleton on the closed month vs LY final — weeks
  within the month, shape of the month (best/softest day, days behind LY,
  rate-vs-volume), month pickup/churn, flags resolved/still open, next
  month OTB at close. Real July 2026 mock on the same canvas.
- 🔄 2026-09-10 **MULTIPROPERTY PORTFOLIO VIEW — SPEC FROZEN (design done,
  build not started).** Working mock (8 test hotels, app components
  transcribed from `firstlight-pwa/web/src`): artifact 4e1faa0b…; design
  suggestions canvas: artifact a34b59d9…. User decisions this session:
  (a) it is NOT a separate page — it is one more entry in the existing
  Hotel picker ("<group name>"), same chrome, same bottom nav
  (Overview · Pickup · Pace · Calendar; NO FL Pulse — no narrative for
  the portfolio, ever); (b) Overview = the app's Yesterday 2×2 KPI cards
  (Revenue · Occupancy · ADR · RevPAR, summed/weighted across hotels) +
  ONE by-hotel table with a Yesterday | MTD | YTD switch, four KPIs as
  value+pill (OTB cell style), tap header to sort, Portfolio total row;
  (c) Pickup = the four window boxes as the slicer (Yesterday · 3-Day ·
  7-Day · 14-Day → needs a `last14d` window in Q3) + Booked/Cancelled/Net
  per hotel; (d) Pace = the app's BarPace/OccPace with a KPI switch
  (Revenue · Occupancy · ADR · RevPAR), full-year table in 3 columns
  (2026 · vs STLY | Final LY · vs final), tap a row → chart that hotel;
  (e) Calendar = DemandHeat cut to 7 | 14 | 30 days, portfolio occupancy
  per stay date, tap a date → by-hotel panel sorted worst-first with the
  red "far behind LY" dot and "N of M hotels behind"; (f) follows the app
  Settings (Gross/Net with the NET strip, reporting year, language);
  (g) stale hotel → amber Data-health banner on top + greyed rows +
  excluded from totals; closed-season hotel → badge, out of occ/ADR
  denominators for Yesterday/MTD, counted in YTD and Pace; YTD occupancy
  and RevPAR over the days each hotel was open. Explicitly OUT: smart
  summary / AI cards, group→company two-level picker, room nights card,
  pickup trend words per hotel, watchlist buttons on the calendar.
  BUILD ORDER when started: fetcher `ytd` block + `last14d` (fail-open,
  schema-tolerant) → `GET /portfolio` (server-side aggregation: sums for
  rev/rn, occ = Σrn/Σavail, ADR = Σrev/Σrn, freshness + closed rules,
  hotels via `hotel_access`; until C4 lands, the user's hotel list) →
  React: picker entry + the five sections above → log + skill note.
- 🔄 2026-08-24 **AI value push: notifications v2 + follow-up memory**
  (brainstorm → user picked top 3; backend afb644f cards-v1.8-followup,
  React 7bc243b, SQL 2026-08-24_notification_types.sql — USER MUST PASTE).
  (1) Intraday pushes, zero-Claude, after 11:00/17:00 UTC data-only runs:
  ALERT = cancel spike today (>=max(8, 3× trailing daily)) or forward month
  slipping <=-5% vs STLY since morning; MOMENTUM = month passes LY FINAL or
  strong booking day (>=max(15, 2× trailing)). Claim-then-send via
  intraday_log PK(hotel,day,type) → hard cap 1/type/day, silent until SQL
  pasted; templates use % + counts only (no-derived-euro rule). Data-only
  runs no longer send the morning-style push (they DID before — 3×/day dupe
  bug fixed). Morning push body = deterministic headline ladder.
  (2) Follow-up memory in the novelty gate (7-day window): repeat cards get
  facts first_flagged + "gap ~N% wider/narrower since"; still-open items
  resurface every 3rd day as "Still open:" instead of silent demotion.
  (3) Settings → Notifications: per-type On/Off (Morning/Alerts/Momentum) →
  push_subscriptions.notification_prefs, sender filters per type
  (schema-tolerant). Tests: test_intraday.py 15 + 17/34/37 all pass.
  ⬜ NEXT: paste SQL; watch tomorrow's 03:30 (headline body) + 11:00/17:00
  (first intraday window); later: group-detection signal, per-card deep-link.
- ✅ 2026-08-24 **7-day history** (React 77ce96f): pill strip above the Smart
  Summary — `Today · Sat 23 · Fri 22 …` from the hotel's last 7 stored
  briefing rows (data already existed; no backend change). Tap = that day's
  FULL briefing (data + that day's AI cards) rendered by the same
  components; amber "Viewing the briefing of …" banner + Back to Today;
  refresh guarded in past view; hotel switch resets to today; feedback
  thumbs still work on past cards (report_date-keyed); `history_view`
  tracked. Past days load via Supabase (Phase A API has no by-date endpoint
  yet — add one before the API becomes the only read path).
- 🔄 2026-08-24 **Usage tracking layer** (React 3d4b670 + SQL
  2026-08-24_usage_events.sql — USER MUST PASTE). First-party events into
  Supabase `usage_events` (user_id/hotel_id/session_id/event/props); RLS =
  authenticated INSERT-own-only, reads service-role only. Client
  `lib/track.ts`: 10s batching + page-hide flush, fail-silent, GATED to
  demo@hbis.io via TRACKED_EMAILS (set to null → track everyone). Events:
  app_open (platform/standalone/viewport), session_end (seconds), tab_nav,
  hotel_switch, refresh_tap, bell_toggle, setting_change, hero_expand,
  voice_play, card_expand, share_tap, feedback_submit. ⬜ NEXT: paste SQL →
  browse demo → verify rows; later a usage digest (daily counts per event)
  + widen gate at go-live.
- ✅ 2026-08-23 Section-header consistency (React 9e04270): every visual now
  uses the same SectionLabel (icon + title + ⓘ + Share) OUTSIDE the white
  card — Yesterday/MTD got icons, MTD/OTB/butterfly/speed/pace-charts/heat/
  bridge/sources titles moved out; "Demand heat" renamed "Next 60 Days
  Demand"; "Cancelled revenue · 7 days" strip removed (user spec).
- ✅ 2026-08-23 **NO DERIVED EURO FIGURES in briefings — standing rule**
  (user, after the hero mis-framed the lead-time signal's €1,079,123
  "revenue in motion" as a "€1.08M opportunity"): only real PMS revenue is
  ever displayed or narrated. cards-v1.7-noderived (89a1fb5):
  `_DERIVED_EUR_FACTS = (value_at_stake, proj_rev_band)` stripped from the
  narration prompt + haystack; Signal-5/projection cards now show the
  %-vs-reference band instead of the € projection band (chips + texts).
  Derived values still drive scoring/floor/novelty internally (_stake_eur).
  INCIDENT note: cards-v1.6-nostake (2544f80, pushed 08-19) was silently
  never deployed — lost in the Railway 08-19 incident — so the 08-23 03:30
  narration still produced at-stake prose. Detected via the €1.08M hero
  sentence; fix: /health now exposes `prompt_version` (469f20c) — ALWAYS
  verify the served analyst version after a backend push. v1.7 deploy
  verified via /health.
- ✅ 2026-08-19 **Euro at-stake estimates removed from ALL display + narration**
  (user: "remove all calculations at stake etc"; backend 2544f80
  cards-v1.6-nostake, React c71ee57). The estimates remain INTERNAL — they
  still drive the significance floor, R-scoring and the novelty gate (shipped
  insights now carry a bare `_stake_eur` number for tomorrow's novelty
  lookup; legacy rows still parse at_stake). Removed: card at_stake field &
  "At stake:" action suffix, hero digest stake + fallback "— €X at stake",
  soft-dates fallback chip sub, value_at_stake facts from the narration
  prompt (Claude can no longer cite them; validator haystack matches).
  React drops the At-stake row and sanitizes legacy stored briefings
  (evidence subs + recommended_action). Old heroes narrated before this
  change still contain the phrase until the next 03:30 run. Tests 17+34
  pass (hero test updated to expect NO stake).
- ✅ 2026-08-19 **Smart Summary v2 — deterministic** (React da3662a; mock
  approved first, artifact a1e6e762). Headline is a client-side rule ladder
  over pre-computed facts (first match wins): ~~cancellation spike (churn >=15%
  & >=10 rn)~~ -> big yesterday (|vsLY| >=15%, "Strong/Soft {weekday}") ->
  **cancellations UP vs prior week (2026-08-28, see release row)** ->
  forward month <= -5% vs STLY ("{Month} needs attention") -> pickup
  accel/slow (net7 vs prior-week net from pickup_daily, +-15%) -> "Steady
  day — MTD {x}%". Sections upgraded: state pills (AHEAD/BEHIND/MIXED,
  SPEEDING UP/SLOWING/STEADY), Pickup shows booked/cancelled/net + WoW
  arrow, On the Books names best + watch months. NO Claude call — updates on
  every refresh (11:00/17:00/manual), follows net mode instantly; the
  once-daily narrated hero remains behind "Read the full briefing" (speaker
  reads it; both hidden if hero missing). Templates do zero arithmetic
  (safety-first) — slots only. EN only until the i18n dictionary lands.
- ✅ 2026-08-19 "Where each month stands" section REMOVED (user request; the
  MonthStands meter is gone from the Pace tab — pace charts + booking speed
  + heat + bridge + sources remain). React 0abd77f + d5adbb3 (the first
  commit shipped with dead code that broke the Pages build; fixed forward).
- ✅ **REPORTING-YEAR SELECTION → SETTINGS** (user spec 2026-08-19: "put it
  also in the settings… reporting year and comparison year"; React 91ea5b2).
  In-body bar removed. Settings rows: `Reporting year [2026 | 2027]`,
  `Comparison year` (2026 → fixed 2025 = STLY + final; 2027 → [2026 | 2025]),
  one-line semantics caption under them. When 2027 is selected a slim
  `2027` pill strip under the hero says what's compared and points to
  Settings. Session-only (opens on the current year). Modules that follow:
  OTB matrix, pickup boxes/butterfly/speed, pace charts; calendar + AI don't.
  Backend Q16 stly2 series serves the 2027-vs-2025 branch.
- ✅ **Settings: Gross | Net revenue toggle** — SHIPPED 2026-08-18 (React
  3364071, served bundle verified). Settings row "Revenue  Gross | Net",
  DEFAULT = GROSS, persisted in localStorage `fl_revmode`. Net view derives
  from the payload: yesterday/MTD `revenueNet(LY)` and OTB pace
  `rev_net/rev_stly_net/rev_final_net` are EXACT (Hitia.logisnet, Q1+Q4);
  net ADR = net revenue ÷ nights client-side. Sections that don't carry a
  net query yet (pickup windows/daily, top channels, next-year pace, ADR
  bridge sources) are scaled by the hotel's MTD net/gross factor so the app
  reads in one basis; a small "NET · Revenue and ADR shown net of VAT &
  taxes" strip appears under the hero. Hero/AI text stays gross (narrated).
  Toggle refuses (toast) until the payload carries net fields.
  FOLLOW-UP ⬜: add logisnet to the pickup / channels / Q16 / bridge queries
  so the estimate goes away everywhere.
- ✅ 2026-08-19 **Instant open** (React 8991bb0): last briefing + hotel list
  cached in localStorage per hotel → painted immediately on open, fresh copy
  fetched behind it (stale-while-revalidate; cache cleared on Sign out);
  session guessed synchronously from the stored Supabase token (no Login
  flash); first-ever open shows the navy header + "Loading briefing…" instead
  of a white page. Top Sources bars 6px → 14px (user: "much thicker, same
  design"), STLY tick 3×20 navy.
- ✅ 2026-09-04 **Login screen — "Pre-dawn"** (React e543a2f; user-supplied
  handoff login-1b-2a-code.html copied verbatim): #0b1530 bg, three aurora
  glows, 9g mark with 2a slow-orbit corona (flSpin 30s, reduced-motion
  off-switch), wordmark First<Light cyan> + "BEFORE THE DAY BEGINS" Plex
  Mono tagline, glass inputs (54px r15, focus cyan ring), gradient submit
  with cyan glow, POWERED BY HBIS footer. Fonts self-hosted per standing
  rule: outfit-var.woff2 (100-900 latin) + plexmono 400/500/600 latin
  (~78KB) added to public/fonts + fonts.css. Functional extras: friendlier
  wrong-password message; "Forgot password?" now sends the Supabase reset
  email (redirect to origin; recovery lands the user signed in — a proper
  in-app new-password screen is a noted gap). Verified live on
  firstlight.hbis.io (bundle BM0neC5Q + font files 200). This closes the
  "app landing page" punch-list item.
- ⬜ **Marketing website for "Xenia"** (working name, user idea 2026-08-18) —
  public landing site with LIVE, anonymized briefing data scrolling as a
  self-running demo (feed from the demo hotels — Azure Bay / Thalassa)
- 🔄 **NOTIFICATIONS — root cause found + fixed 2026-08-19** (user: "I still
  don't get any notification even though I clicked the bell"). Three
  independent causes, all confirmed by probe, all fixed:
  1. React bell had NO push code (local on/off + toast only) → now real Web
     Push: `web/public/sw.js`, `src/lib/push.ts` (VAPID subscribe, row per
     hotel via delete+insert, unsubscribe), bell state = browser subscription
     AND server row **for the hotel in view** (per-hotel tick ✓ green / × red
     badge), iOS "add to Home Screen first" hint, notification tap → AI
     section. React 9be43c1 / fad982d / 5612f3f.
  2. `push_subscriptions` was EMPTY (0 rows): the legacy upsert on
     (user_id,hotel_id) 42P10'd because that index never existed — AND the
     table carried `push_subscriptions_user_id_key` = UNIQUE(user_id), i.e.
     one subscription per USER not per hotel → a 2-hotel user could only ever
     be notified for one hotel. `docs/sql/2026-08-14_push_subscriptions_unique.sql`
     rewritten (drop per-user unique, add unique (user_id,hotel_id)); user
     pasted 2026-08-19 ~00:55 Athens; verified: 2 rows now (Pome + Potidea,
     endpoint web.push.apple.com = iPhone Home-Screen app).
  3. No way to test without waiting for 03:30 → `POST /push/test?hotel_id=`
     (Bearer hotel token) sends a test push to that hotel's subscriptions
     (api.py 9408f12). Deploy blocked by RAILWAY INCIDENT "Deployments are
     slow to progress / prone to timeout" (3 Snapshot-code timeouts, then a
     build stuck >40 min; status.railway.com Identified 01:16 UTC). Old
     process keeps serving fine. NEXT: when deployed, fire /push/test for both
     hotels (scratchpad push_test.ps1), then the 03:30 run is the real test.
  Lessons: probe the table (row count + constraints) before trusting any
  client "saved" message; a UNIQUE on the wrong key is invisible until the
  second hotel. Legacy Vercel bell also benefits from the SQL fix.
- ⬜ **Notification rules — check on real phones**: exactly when/what a user
  gets notified (today: 03:30 scheduled run only, manual/data-only silent;
  needs the React bell subscribed; verify iOS + Android)
- ⬜ **AI insights — re-review**: quality pass on card ranking, wording, caps
  and fallbacks against the accumulating 👍/👎 + notes (real + demo testers)
- ✅ 2026-09-04 **Share = IMAGE, per section** (React 2fd969a). Every ↑Share
  pill captures ITS section (html-to-image, pixelRatio 2, iOS warm-up
  double-render, the pill itself filtered out) and composes a branded PNG:
  #EAEDF1 frame, header hotel name + section title + report date, footer
  app icon + "FirstLight · firstlight.hbis.io". Native share sheet with
  files (WhatsApp path) when canShare({files}); else download; link-share
  only as last-resort fallback. data-share-root wrappers added for the
  fragment sections (Pickup Activity / AI Insights / Pace); all other
  sections resolve via the label's parent wrapper. share_tap now carries
  mode:'image'. ⬜ VERIFY ON REAL PHONES (iOS foreignObject rendering is
  the known risk — test WhatsApp share of Yesterday + a Pace chart; report
  any blank/partial captures). Preview-artifact caveat: single-file preview
  can't fetch /fonts, capture may degrade there — live app is the test.
  (superseded item:) React share previously sent a link; port the
  old app's capture-to-image share, scoped to specific chart/section blocks
  (share pill on each section captures THAT block)
- ✅ 2026-09-06 **Mobile shell v2.2** (React c21e72b, from the user-supplied
  MOBILE-SHELL-UPDATE.md spec; scope decided item-by-item by the user:
  "1.4.5 build them, 2 no, 3 already live, 6 keep the live one, 7 build,
  8 build as bugfix, 9 not yet, 10 build"):
  §1 bottom tab bar — fixed, safe-area padded, 19px stroke icons per tab,
  gradient active indicator, FL Pulse count badge (kept CONSTANT — §2 "pulse
  once on new insights" DECLINED), haptic tick; top tab row deleted; main
  padding-bottom 84px+safe-area. Pull-to-refresh wrapper now transforms only
  while pulling (a permanent transform would have trapped the fixed bar).
  §4 collapsing header — scrollY>60 collapses (expand <20, hysteresis): bare
  22px mark + hotel name (ellipsis, picker tap scrolls up first) + mini ↻;
  bell/share hidden; row 2 max-height/opacity animated.
  §5 day strip — 44px right-edge fade; past-day banner restyled to spec
  strip (#FBF3DF/#EDDCA8/#6D4C00, clock icon, "Viewing Fri 4 Sep · not
  live" + Back to Today).
  §7 loading/offline — fl-shimmer skeletons on first load AND ~1.5s on
  manual refresh; fetch-fail-with-cache shows grey "Offline · showing
  briefing as of {date}" strip instead of an error.
  §8 BUGFIX (user-reported): switching hotels now resets the active tab to
  Overview + scrolls to top (chart selections already reset per-hotel).
  §10 viewport-fit=cover for edge-to-edge safe-area.
  DECISIONS RECORDED: §3 FL Pulse restyle + §6 chart shells already live —
  §6 keeps our pill-top bars over the spec's rx3 (user: "keep the live
  one"); §9 tablet side-rail NOT built (placeholder comment in Shell.tsx).
  FOLLOW-UPS same day (React 232c23a → f905300, live CDErRH9z): labels
  10→11.5px (user: bigger); then FINAL nav per user-supplied
  bottom-nav-final.html — outline→FILLED icon swap on the active tab
  (label 800, gradient indicator top -9, badge hides at 0, wraps the icon),
  and the NEW Pace icon B (this-year bars solid / last-year hollow)
  applied BOTH in the nav and in ICONS.pace (Pace section header) — the
  two must stay identical (comment in Shell.tsx). Verification lesson
  re-learned: grep the LIVE bundle for a string unique to the new commit
  (the pace polyline + clock paths exist elsewhere → two false-positive
  "verified" calls before the real one). THEN frosted glass (React f0b7f33,
  user-supplied bottom-nav-frosted.html): bar = 72% white + 14px backdrop
  blur (content scrolls visibly underneath), .fl-tabbar class in index.css
  with -webkit- prefix + @supports solid-white fallback; padding 10px,
  indicator -11.5. The spec's 1.5px gradient top hairline was REMOVED next
  commit (95ce18a) — user: "i dont need this" — then replaced with a plain
  grey 1px #DDE3EC top border (b54e407, user: white-on-white needed
  separation). FINAL FORM (bfa9b93, user screenshot): FLOATING PILL —
  inset 12px, radius 22, full 1px #E2E7F0 border, soft shadow, blur kept
  (78% white); main bottom padding 96px. Lowered on user feedback
  (c41c7fb): bottom = 4px + HALF the safe-area (was 12px + full).
  BUGFIX (2701ed1, user: "menu hidden a bit when scrolling"): nav now
  createPortal'd to <body> — the pull-to-refresh wrapper transforms while
  dragging, and a transformed ancestor turns position:fixed into
  ancestor-relative, dragging the pill off-screen during the gesture.
  Rule: NOTHING position:fixed may live inside the pull wrapper.
  HEADER: tried direction-based expand-on-scroll-up (b5b7be9) — REVERTED
  same day (329fdf9): a screen tap caused tiny scroll jitter → header
  flashed open/closed ("confusing" — user). Final: original hysteresis,
  collapse >60px / expand <20px near the top. Don't re-propose
  direction-based without a tap-jitter guard.
  LIQUID GLASS (249b97c, 2026-09-08, user: iOS-26 Liquid Glass tab bar):
  bar = 55% white + blur(18px) saturate(1.7), rgba border, inset specular
  top highlight; NEW .fl-lens — glass bubble under the active tab, width
  20%, left animates .38s springy cubic-bezier (morphs across on tab
  switch, reduced-motion: none), chromatic conic-gradient edge ring via
  mask-composite; gradient top indicator REMOVED (lens replaces it).
  Web limits noted to user: no true refraction/warp, slide not Apple's
  stretch-merge morph. POLISH (7b2641f, user: selection fired on finger
  LIFT, wants native touch-down + rounder): tabs select onPointerDown
  (guarded onClick e.detail===0 keeps keyboard); lens radius 999px
  capsule (REVERTED to 19px same day, 37112ef — user), slide .38s→.28s, then .45s gentler spring (75bddd7 — user: slower, like iOS), then
  LIQUID MORPH (85762fa, user: multi-tab jumps didn't slide nicely; wants
  Apple's glassEffectID blob): lens rests via inline translateX, travel is
  a WAAPI keyframe morph — stretches scaleX 1.3-1.9 by |distance| with
  slight Y-squash mid-flight, micro-overshoot settle, duration
  420ms+70ms/tab; prefers-reduced-motion skips; CSS left-transition
  removed. True refraction over passed icons = impossible on web (told
  user). Verification lesson #3: 'onPointerDown' is a
  USELESS bundle marker — react-dom contains every event name; match the
  hash against the local build or grep a truly unique literal.
  ICON SYNC (8935932, user): Next 60 Days Demand section header now uses
  the Calendar tab's calendar icon (new ICONS.cal60, replaces the heat
  grid) — Pace/Calendar/FL Pulse are matched nav↔section pairs;
  Overview(sun)/Pickup(trend) intentionally keep their report identities.
- ⬜ Dynamic translation: i18n dictionary layer (en/el, ~150 keys) wired to
  the EN/ΕΛ switch — instant UI translation; narration stays per-hotel
  next-morning (cost policy)
- ⬜ Notification rules audit: verify what arrives on the phone and when
  (03:30 only, content format per the 3-type design: briefing/alert/momentum)
- 🔄 MULTIPROPERTY PORTFOLIO VIEW: spec frozen 2026-09-10 — see the
  dated entry in the TO-DO list above (picker entry, five sections,
  server-side `GET /portfolio`, what is explicitly out)

**USER (updated 2026-08-10):**
- ✅ 2026-08-10: ALL pending SQL pasted + verified (insight_feedback, hotel_prefs,
  hotels.season_settings, refresh_runs.attempt) — thumbs + EN/ΕΛ selector live
  in the app from this moment; season_settings null for both hotels awaiting
  the user's own Protel rooms/season queries
- ⬜ Fill season_settings dates + real-rooms query (user took this)
- ⬜ Create `firstlight_ro` read-only login on Pome SQL Server, send password →
  swap pms_config off `sa` (security must-fix)
- ✅ Word-caps decision RESOLVED 2026-07-27 (cards-v1.4-singleshot) — user
  cost policy: NO retries, every Claude call costs. Narration = ONE attempt;
  a validation miss ships the free deterministic fallback card. Prevention
  moved up-front: word budgets embedded in the tool schema field descriptions
  (write ≤80% of cap; over-limit discarded), same for hero. Surgical
  retry-feedback code kept dormant behind NARRATION_ATTEMPTS env (default 1).
  Expected: cost drops to ~$0.04-0.05/hotel/day flat; watch fallback rate —
  if it climbs above ~1 card/day, relax caps +3 words (free fix).

**Pilot week (→ ~2026-07-31):**
- 🔄 Daily refresh_runs check — Claude (days 1-4: zero tunnel errors; day 4
  degraded on word caps, not infrastructure)

**End of pilot week — Phase 3 (closes the migration):**
- ✅ 2026-08-10 Potidea on the tunnel (route + service token, pms_config, first run OK)
  ⬜ decommission old daemon + tasks on the Potidea server (user, RDP) (kills the stray 04:00 trigger; Potidea
  gets Signal 3 + hero automatically)
- ⬜ Delete FirstLight code folders from BOTH hotel servers → migration complete

**Small builds (Claude, anytime — pilot-safe):**
- ⬜ Signal 3 polish: exclude comp/house sources from drill-down;
  `pms_config.lead_window_days` (default 28)
- ✅ Chart fix 2026-07-27: OCCUPANCY line chart — closed months (month_num <
  current month) show STLY only; the Final LY dashed line starts at the current
  month. Revenue + ADR bar charts deliberately unchanged (user choice).
  Legacy payloads without month_num keep the full line (guarded).

**Then — the stack migration (sequenced, each phase shippable + rollback-able):**
- ✅ 2026-08-13 **Phase A — FastAPI** LIVE (web-cloudflare.up.railway.app): uvicorn 1 worker,
  scheduler → lifespan, port /trigger + /briefing/latest, per-hotel API tokens,
  kpi_summary column + GET /briefing/history
- 🔄 **Phase B — React on Cloudflare Pages** (LIVE at firstlight-pwa.pages.dev, parallel-run with Vercel; go-live sequence pending): audit PWA repo first;
  render-from-data; new card anatomy + hero block + Greek/English + text-size
  1-5 + bigger OTB charts + closed-month LY fix + scroll fix + 7-day history UI;
  reads via FastAPI tokens; parallel-run then retire Vercel; drop rendered_html
- 🔄 **Phase C — Postgres on Railway** (C1 dual-write OPEN since 2026-09-04;
  C2 endpoints live): reads flip → LISTEN/NOTIFY queue → app via API →
  **C3 own login system + C4 Group→Company(VAT)→Hotel tenancy + C5 no email
  (decided 2026-09-10, see PHASE_C_RUNBOOK)** → retire Supabase entirely
- ⬜ Phase 4 scale prep before hotel #10: de-globalize config →
  REFRESH_CONCURRENCY=10, load test 20-30 hotels, per-hotel briefing time/tz

**PMS INTEGRATIONS ROADMAP (added 2026-08-18):**
- On-premises — same process as Protel (tunnel + read-only SQL login + new
  adapter folder filling the HotelDataSnapshot contract):
  - ⬜ **Pylon** (SQL Server, similar reservation-line model — easiest, do first)
  - ⬜ **Opera 5** (Oracle; RESERVATION_NAME + RESERVATION_DAILY_ELEMENT_NAME,
    status codes, revenue buckets → filter to room revenue; real port not rename)
- Cloud — no SQL/tunnel; WE pull, store, compute. Confirmed by user: APIs give
  **book date + cancellation date + last-modified** → STLY reconstructs right
  after a 2-year backfill; incremental sync via modified_since:
  - ⬜ **Opera Cloud** (OHIP: OAuth, per-property app registration, partner
    onboarding lead time — start early)
  - ⬜ **Hotelizer** (lighter API, same model)
  - shared piece: **reservation store + incremental sync engine** in our
    Postgres → Phase C is a PREREQUISITE for cloud PMS; per-PMS = mapping to
    canonical reservation row, then Q1–Q16 run over our schema
  - discovery per cloud PMS (2 days, sandbox creds): exact cancellation
    semantics (do cancelled rows keep stay dates/rate?), how modifications /
    rebooks appear (new record vs update)
- ⬜ **SEMANTICS.md FIRST** — PMS-neutral definitions (room night, room revenue,
  STLY, cancellation in/out, comps, fake rooms, season) every adapter must
  satisfy; write before adapter #2

**Product roadmap (parallel to stack work, user picks order):**
- New card types (compute-only): ADR-vs-occupancy trade-off · cancellation spike
- Insight feedback loop (agreed 2026-07-27): 👍/👎 per card + reason chips →
  `insight_feedback` table + POST /feedback in Phase A; UI in Phase B card
  footer; Tier-1 learning = bounded per-hotel ranking-weight tuning (N
  component), pattern-gated (≥5 signals, decay). GUARDRAIL: feedback tunes
  ranking/phrasing only — NEVER suppresses hard-gated facts (shoot-the-
  messenger protection). Tier 2 editorial prompt notes later; Tier 3 only at
  scale. Joins on refresh_runs.cards_audit by card_id.
- Onboarding kit → onboarding agent (agreed direction 2026-07-27): v1 kit
  (~2-3 days): install-cloudflared.ps1 + SQL discovery probe (Hitia check,
  mpehotel list, zimmer count, fake kat detection) + auto-proposed pms_config
  + GM verification report — dogfood on Potidea/hotel #3. v2 (pre-scale,
  ~1-2 wks): Cloudflare API provisioning + email flow + LLM diagnostic loop
  (agent handles weird cases; humans keep: run installer, sign off numbers).
- db/adapters/SEMANTICS.md — PMS-neutral metric definitions (STLY, cancellation
  in/out, fake rooms) to write BEFORE adapter #2; ~1h, knowledge fresh now
- Follow-up loop (advice → outcome tracking) · chatbot · monetization tiers

---

## 5. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-09-12 | **Opera revenue INCLUDES posting-master (pseudo PM/PI) postings; nights exclude them** | User: "there is big difference we should be the same" — City Hotel Jan 2026 checked day by day against Hotel BI: every differing day = exactly the revenue posted on pseudo rooms (3,585 € in Jan); with it included the month is 275,469 € to the euro and nights are unchanged (posting masters carry no nights). Room revenue moved to a paymaster is real revenue with no physical night |
| 2026-09-12 | **Opera adapter = one extract + Python pack, not 16 SQL queries** | The BI view is a heavy UNION; per-query scans cost 5–15 s each. One grouped extract costs 1–3 s per property and lets the Protel conventions live in one tested Python module (`compute_pack`). Bounded window, stateless — same rule as the SQL pack. Lesson: NEVER filter `psuedo_room_yn` in the WHERE of that view (27 s vs 2 s — predicate pushdown into the union); keep it in the SELECT CASE |
| 2026-09-12 | **Shared payload assembly for all adapters (`db/adapters/assemble.py`)** | Adapter #2 would otherwise duplicate 300 lines of derivation (ADR/occ/curves). Protel fetcher moved to it verbatim; behaviour identical, all existing tests pass. Adapter #3 (Pylon/Fidelio) only has to produce the Protel-shaped rows |
| 2026-09-10 | **Portfolio view = an entry in the Hotel picker, not a new page; zero narrative.** Same chrome, nav and components as the single-hotel view; Overview cards + ONE by-hotel table (Yesterday/MTD/YTD), pickup boxes as slicer, Pace with KPI switch + 3-column year table, Calendar 7/14/30 with by-hotel panel; follows Settings (Gross/Net, reporting year). Aggregation is server-side (`GET /portfolio`): sums for revenue/nights, occupancy = Σnights/Σavailable, ADR = Σrevenue/Σnights — never averaged percentages; stale hotel out of totals + amber banner; closed hotel out of occ/ADR denominators | User: "the layout should be exactly the same … one more option will be portfolio on the drop down … no smart summaries for the multiproperty view"; then "yes to all design changes", content: follow app settings, no two-level reporting, no room nights, no pickup trend words. Server-side aggregation so every screen and any future portfolio push agree on one number |
| 2026-09-10 | **Supabase goes away COMPLETELY, including Auth** — own login system in our Railway Postgres (`users` / `sessions` / `memberships`, argon2id, opaque session tokens, forced password change on first login, admin-issued passwords). Phase C3 rewritten (was "keep Supabase Auth") | User decision ("own login system"). One dependency fewer, one auth path in the API, and with no email channel Supabase's magic-link/reset value was nil anyway |
| 2026-09-10 | **Tenancy hierarchy: Group → Company (unique VAT per country) → Hotel.** Users are attached at ANY level via `memberships`; `hotel_access` view resolves access. Merges NEVER delete: same owners → `groups` row; hotel changes hands → `update hotels set org_id`; same VAT twice → move hotels to the survivor, deactivate the other | User: "company unique VAT, hotels, and sometimes we will merge companies and hotels because they are owned by the same people" + "a parent group, yes". History always stays on the hotel id |
| 2026-09-10 | **No email channel, ever.** Morning briefing = push + app only. `recipient_email`/`recipient_name` deprecated; intake no longer asks for a GM email; initial passwords handed over by phone. Ops audit/drift email to HBIS stays (monitoring, not a client channel) | User decision ("we won't send any emails") |
| 2026-09-10 | **Onboarding runbook v2** ([ONBOARDING.md](ONBOARDING.md)): intake adds company VAT + legal name, shared-owners question, user list with levels; database step = one admin script (`scripts/onboard_hotel.py`, TO-DO) writing company → group → hotel → users in one transaction; verification logs in at firstlight.hbis.io as a NEW user and expects ONE push, NO email; §4 merges & moves table. Until C3/C4 ship the DB step still runs in Supabase as v1 | Same session; schema in `docs/sql/pg/2026-09-10_tenancy_auth.sql` (+ schema.sql), skill `hotel-onboarding` updated |
| 2026-08-10 | HERO DRIFT RESOLVED — Option A (keep as is): the hero paragraph stays a morning snapshot; intraday KPI drift (room-revenue value edits after 03:30, e.g. €81,598 vs €81,808) is explained by the "Last refresh" timestamp in the Smart Summary header + the ⓘ hero explainer. NO regeneration on data-only runs | Drift is cents-level and legitimate (both numbers correct for their moment); options B (drift-triggered regen ~$0.005) and C (always regen ~$0.015/day) rejected under the cost policy — zero extra Claude calls |
| 2026-07-28 | Pricing: FirstLight = **€99/hotel/mo + VAT, flat, unlimited users** — never per room count. Sold as add-on to Hotel BI (€330), standalone, or €399 bundle. Full commercial policy in [COMMERCIAL.md](COMMERCIAL.md) | Value unit is the briefing (one/hotel/day), COGS flat (~€2–3/mo AI); flat matches Hotel BI's per-property model; unlimited users spreads the habit through the hotel. Only size lever: portfolio discount from 2nd property |
| 2026-07-30 | ONE PICKUP TRUTH (user decision): Q9 pickup_daily + Q14 cancel_daily count ALL stay dates (restriction `date > today` + 1yr cap removed) — bookings for consumed nights (walk-ins/same-day) included, so the butterfly RECONCILES EXACTLY with the Pickup Activity card (Q3, same book-date-axis convention). Butterfly/velocity share calendar-aligned 7/14d windows anchored on newest booking date. Analyst Signal 1 guards `m_end < today` so finished months never become cards. Audit trigger: user found +147rn booked / −7rn cancel gaps; root causes = future-only scope + an 8-day distinct-date cancel window |
| 2026-07-26 | Data layer end-state: Supabase → **Railway managed Postgres** at the overhaul release (NOT Postgres-in-container — ephemeral FS). FastAPI becomes the ONLY gateway (PWA stops reading the DB directly; needs per-hotel auth). Rationale: once render-from-data routes the PWA through our API, PostgREST value evaporates; colocation + one less external dependency. Migrates together with React/Pages + render-from-data as ONE coordinated release |
| 2026-07-26 | Scheduling in the FastAPI container: APScheduler in the lifespan hook, **exactly 1 uvicorn worker while scheduler lives in the API process** (N workers = N schedulers = duplicate briefings/emails). At scale: web/worker split — same image, two Railway services; queue = refresh_commands with FOR UPDATE SKIP LOCKED + LISTEN/NOTIFY. Per-hotel briefing times = per-hotel APScheduler crons (tz-aware) |
| 2026-07-26 | NO Celery: ~1k tasks/day at 200 hotels doesn't justify a Redis broker (new always-on dependency in the 03:30 path, Windows dev pain, re-buys locks/retries/audit we have). Postgres-native queue at the Phase 4 split; **Procrastinate** (Postgres-broker task queue) if we want task ergonomics. Revisit only at >50k tasks/day or multi-machine workers (chatbot-driven) |
| 2026-07-26 | Concurrency truths: reads scale (precomputed briefings); duplicate refreshes collapse to 1 run (atomic claim + per-hotel lock + AI reuse); fleet-morning compute is the only real 100-at-once problem → Phase 4 (de-globalize config → REFRESH_CONCURRENCY=10 ≈ 15 min/100 hotels; per-tz staggering) |
| 2026-07-26 | TARGET ARCHITECTURE (agreed w/ user + CTO input): three layers — Cloudflare = network + frontend (DNS, tunnels, Access, React PWA on Cloudflare Pages after the overhaul; Vercel retires); ONE Docker container = compute (FastAPI adopted when the history endpoint lands, + scheduler + analyst); Supabase = data. Container host (Railway today) is a swappable landlord, revisit at ~20 hotels (DO App Platform / Fly / CF Containers when mature). Backend can NEVER run on CF Workers (pyodbc native driver, cloudflared subprocess, long-running scheduler). Repos stay separate (agent vs PWA) — IP isolation + independent deploys; one VS Code workspace for cross-repo work |
| 2026-07-24 | Claude runs ONCE per hotel per day (scheduled morning run only); data-only AND manual refreshes reuse the day's AI insights (`2b91f6e`) | Data barely moves intraday; per-refresh regeneration was pure cost. Hard-caps AI at ~€2-3/hotel/mo; manual refresh drops ~100s → ~10s. Fallback: refresh before any morning AI → generates fresh |
| 2026-07-24 | Manual refreshes are silent — no email, no push (`5402b37`) | A debugging day sent the GM 6 briefing emails; notifications/email only from scheduled runs |
| 2026-07-24 | Pome server decommissioned to cloudflared-only (`8e1329e`) | Cloud command poller + 03:30 UTC schedule replaced the daemon's last two roles; refresh-button test passed with zero hotel-side code |
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
| 2026-08-10 | Pome tunnel fetch dead ~03:30→14:45 Athens (5 failed runs, 08001 prelogin handshake; briefing frozen at morning snapshot — gate held, nothing corrupted) | An **RDP browser-rendering connection rule** (`connection_rules: {rdp: {}}`) landed on the Pome SQL Access app during the same-morning dashboard session that created Potidea's app — Access then 403'd the plain TCP tunnel client. Correlated misleadingly with the Phase A deploy minutes earlier | Rule removed → instant recovery (14:45 run success, today-pickup live, kpi_summary flowing). Diagnosis chain worth keeping: container suspected first (2 blind fixes: OpenSSL TLS floor + Driver 17-first fallback — both KEPT, harmless hardening) → Potidea green on same container exonerated it → local tunnel repro failed while direct SQL worked → edge probes: bridge 502 both hotels (connector up) + Access probe 403 Pome / 200 Potidea = smoking gun. Lessons: (1) when two things change the same morning, test BOTH independently before fixing either; (2) `Invoke-WebRequest` with CF-Access headers is a 10-second Access-layer test — use it FIRST for tunnel failures; (3) touching the Zero Trust dashboard for hotel N can break hotel N−1 — after any Access change, probe every hotel's hostname |
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
| (local, not pushed) | 2026-09-12 | **OPERA 5 / ORACLE ADAPTER v1 — `db/adapters/opera_oracle/` (user: "please follow same logic for cancellations as for protel").** Source = the Tor group's own BI view `OPERA.EUROTEL_TARGIT_WBLKNM` (per reservation × stay night; the view their Power BI reads). Design: ONE bounded extract per property (`Q_GRAIN`: stay × book × cancel date × source × status group, 13 months back → next year end, 42–109k rows, **1–3 s**) → `fetcher.compute_pack()` recomputes every Protel query (Q1–Q16, same column names, same STLY/cancellation/lead-time/consumed rules, `_alive_at` = `reschar<2 OR Canceled>cap`) → NEW shared `db/adapters/assemble.py` builds the payload (extracted verbatim from the Protel fetcher; `protel_mssql/fetcher.py` is now query-only and calls the same assembly — 17+37+53+12 existing tests unchanged). Night-audit gate (`OPERA.BUSINESSDATE.STATE = CLOSED` for yesterday, else raise → retry ladder). `db/connection.connect_oracle` (python-oracledb THIN, `oracledb==26.0.0`, no client libs); `railway_main._fetch_via_tunnel` picks the driver from the adapter's `CONNECTOR` attribute; `sql.database` = Oracle service name; `sql.pms_hotel_id` = RESORT code. **Validated live over VPN (`scripts/validate_opera.py`, ASOF 2026-09-11) on all 3 properties:** contract complete, legacy off, all signal fields populated; yesterday == `RESERVATION_STAT_DAILY` to the euro; pickup daily series reconcile with the card; **full-year revenue == Hotel BI "Quick Insights" EXACTLY for Excelsior (1,488,612 €) and ON Residence (3,283,751 €), City 3,441,756 vs 3,442,682 (bookings since their refresh); nights identical (8,090 / 14,343 / 27,843 vs 27,848); STLY within 0.2%** (their cancelled-night pricing = daily rate, ours = `CLX_ROOM_REVENUE_GROSS`). `test_opera.py` = 28 synthetic-grain checks of the conventions. NOT yet: hotels rows in PG (waiting for VAT/rooms/users), push/deploy |
| React (pace 3) | 2026-09-10 | Portfolio preview round 5 (user: "now its too tall, make the columns a bit thinner and the letters for var bigger"). Tall plot 262px (was 330), tall bars 10px (was 13); a variance pill narrower than 54px keeps 12.5px text and only trims its box (text used to scale with the box); occupancy chips likewise. Mock updated |
| React (pace 2) | 2026-09-10 | Portfolio preview round 4 (user: "no i want row only" / "one"). Two-half layout reverted. `BarPace`/`OccPace` gain a `tall` prop (plot 330px vs 172px in the 560 viewBox, ≈1.9× taller); variance pills scale to the month step (`min(54, step−3)`) so twelve never overlap; the portfolio chart bleeds 10px into the card padding for width. Hotel view unchanged. Mock updated |
| React (pace) | 2026-09-10 | Portfolio preview round 3 (user: "the pace chart should be much bigger in height also and width the content should fit better"). Twelve months across a phone-width card cannot carry readable pills (≈27px per month on screen), so the portfolio Pace chart now draws the year as TWO stacked halves — Jan–Jun and Jul–Dec — each a full-width `BarPace`/`OccPace` on ONE shared y-scale (`BarPace` gained an optional `mx`); pills, chips and month labels back at full size, chart twice as tall; revenue axis formats millions as €x.xM. Hotel view untouched (its >8-month scaling from round 2 stays). Mock updated the same way |
| React (fixes) | 2026-09-10 | Portfolio preview round 2 (user: "yesterday word is falling to the next slicer, same for revenue/occ slicer; pace chart variances one on top of the other for a full-year hotel; hotel selection on the pace table should be the gradient border"). (1) `Seg` = CSS grid of equal `1fr` columns (every column as wide as the widest label) — the flex `1` version split the width equally and "Yesterday" overflowed into "MTD"; card headers wrap. (2) `BarPace`/`OccPace`: when months > 8 the 54px variance pills, 44px occupancy chips and month labels scale by 0.74 / 0.85 — at a 40px step they overlapped; this also fixes 12-month year-round hotels in the single-hotel view (latent defect). (3) Selected pace row = the pickup boxes' animated gradient ring (`ringStyle`), rows carry a transparent 1.8px border so nothing shifts. Mock artifact updated the same way. `tsc -b && vite build` clean |
| React 8570efb | 2026-09-10 | **PORTFOLIO PREVIEW LIVE IN THE APP (admin only, fictional data).** User: "can we build it with fictional data, so I can test on the real app? … add for me as admin user an option to switch to portfolio view". `firstlight-pwa` commit 8570efb → Pages auto-build → firstlight.hbis.io. New `fixtures/portfolio.ts` (seeded generator, 8 test hotels, `PORTFOLIO_PREVIEW_EMAILS = ['dk@bi-automations.com']`) + `components/Portfolio.tsx` (Overview cards + ONE by-hotel table with Yesterday/MTD/YTD + header sort; pickup boxes as slicer + by-hotel rows; Pace with KPI switch reusing the real `BarPace`/`OccPace` (now exported) + 3-column year table with tap-to-chart; Calendar 7/14/30 with by-hotel panel worst-first; Data-health banner for the stale hotel; NET strip when Settings = Net). `Shell` gained an optional `tabs` prop (portfolio = 4 tabs, lens width follows). App.tsx: picker gets "Portfolio · preview" after the real hotels for gated emails; `hotelId === 'portfolio'` skips briefing/push/tracking/watchlist fetches; Settings sheet works (Net allowed without a PMS net figure — fixture divides by 1.13). Segmented controls = Settings colours + tab-bar lens morph (user: "slicers should be same style as navigation for the movement but the selected one should be as it is in terms of colours and background"). `tsc -b && vite build` clean; oxlint crashed locally (node error, not a lint finding). NEXT: user tests on Pome/Potidea accounts → feedback → then the real build order (fetcher `ytd` + `last14d` → `GET /portfolio` → swap the fixture for the endpoint) |
| (design) | 2026-09-10 | MULTIPROPERTY PORTFOLIO VIEW — design session, no code. Started as a proposal discussion (freshness guard, server-side aggregation, across-the-group flags, next-7-days, cancellations beside pickup, closed-season rule, portfolio push, channel mix, share-as-image) → user redirected to "exactly the same layout as the app, Portfolio in the hotel dropdown, no smart summary" → mock rebuilt three times, final one transcribed from the React sources (Shell/Overview/Pickup/Charts.tsx: lockup B, icon cluster, picker, liquid-glass bottom nav, KpiRow, OTB cell grid, pickup ring boxes, BarPace/OccPace, DemandHeat) → `/design` canvas with 3 phone boards of suggested changes → all 7 design changes accepted and folded into the mock (one by-hotel table with Yesterday/MTD/YTD, Data-health banner, short names + header sort, 3-column pace table, worst-first calendar panel, €M fallback on cards, group name in the picker) + Settings sheet Gross/Net driving every figure. Content decisions and the build order are in the TO-DO entry and the decision log. Artifacts: mock 4e1faa0b…, canvas a34b59d9… |
| (docs) | 2026-09-10 | ONBOARDING v2 + PLATFORM DECISIONS (user: "no Supabase, we will use PostgreSQL; we won't send any emails; users per hotel or company account — company unique VAT, hotels, sometimes merged because owned by the same people; firstlight.hbis.io is the public domain" → follow-ups: "own login system", "a parent group, yes"). Delivered: `docs/sql/pg/2026-09-10_tenancy_auth.sql` (groups; organizations.group_id/vat_number/legal_name/country + unique (country, vat); users/sessions/memberships; `hotel_access` view; deprecation comments; admin recipes) mirrored into schema.sql; ONBOARDING.md rewritten (hierarchy table + rules, intake 0.1–0.10, DB step as one admin script, verify as a NEW user at firstlight.hbis.io with ONE push / NO email, §4 merges & moves); PHASE_C_RUNBOOK C3 rewritten to own auth + new C4 tenancy + C5 remove email, rollback + 14-day rule moved to after C3; skill `hotel-onboarding` updated; 4 decision-log rows; TO-DO block. NOT touched: application code (api.py still Supabase JWT + hotel_users; mailer still present) — that is C3–C5 work |
| PWA `d614928` | 2026-08-28 | WATCHLIST RANGE STATUS v1.2 (user: "Pome shows Steady for Aug 30–Sep 3 — did we get no nights?"). ROOT CAUSE: range status compared occupancy points vs yesterday with a ±2pt gate = 17 net rooms/day for a 5-night Pome range → a close-in range always read STEADY even with real pickup (the actual net was always on line 2). NEW RULE (`watch.ts rangeLine`): today's net rooms vs what LAST YEAR gained on the same day at the same lead time (`rn_stly` is same-lead-time OTB, so yesterday→today Δrn_stly = LY's net for that day); IMPROVING/GETTING WORSE when the difference ≥ tol = max(2, 0.5% of range room-nights) (Pome 5 nights → 4 rooms). Line 2 now: "+4 rooms since yesterday (last year: +9 on the same day) · lowest date …". Also fixed: range whose end date == report_date showed "Beyond the 90-day window" for one morning (Q10 starts today; report_date = yesterday) → now "Dates have passed". GAP FOUND: spec §5.5 "auto-removed after that day" is not implemented — closed cards persist until Remove (TO-DO). Spec updated (§5.2/5.3/5.5). INCIDENT (self-inflicted, docs only): a `sed` fix for escaped backticks in 586eb1e matched GNU sed's `\`` start-of-buffer anchor and prefixed ~700 log lines with a backtick; repaired in this commit by diffing against ebe1e5f |
| `586eb1e` + PWA `f813c55` | 2026-08-28 | SMART SUMMARY HEADLINE — cancellation rule fixed (user: "why do both hotels say watch out cancellations every day?"). ROOT CAUSE: rule 1 of the headline ladder fired on an ABSOLUTE ratio (cancelled7 / booked7 >= 15% and >= 10 rn). Both sides count all stay dates, so in late season new bookings dry up while cancellations of months-old bookings keep arriving at a normal rate → ratio always > 15%; 10 rn is < 1% of weekly capacity; rule sat first so it hid "Strong {weekday}". NEW RULE (backend `intraday.headline()` + new `cancel_weeks()`; app `rw()` must mirror — JS diff below): fires only when cancellations are UP vs the hotel's own prior week: `cancelled7 >= 1.5 × prior7` (prior7 = Q14 `cancel_daily` rows dated today-13..today-7; last7 = Q3 `cancellations7d` so it reconciles with the Pickup card) AND `cancelled7 >= max(10, 3% of weekly capacity)` (Pome 35 rn, Potidea 50) AND churn >= 15% (secondary). Moved BELOW "Strong/Soft {weekday}". No Q14 → rule silent. Text: "Cancellations up — 30 rooms out this week, vs 14 the week before." (plain-language rule). test_intraday.py 20 checks incl. the late-season trap (high churn, normal level → silent). APP SIDE — DONE in PWA `f813c55` (SmartSummary.tsx `headlineFor`/`computeFacts`, `npm run build` clean; oxlint binding broken on this machine, pre-existing): add `priorCancelled7` = Σ cancel_daily.cancel_rn where report_date−13 ≤ ref_date ≤ report_date−7 (null if no rows) and `weeklyCap = total_rooms*7`; reorder: ydVar rule first, then `priorCancelled7!=null && cancelled7>=Math.max(10,Math.round(weeklyCap*0.03)) && cancelled7>=1.5*Math.max(priorCancelled7,1) && churnPct>=15` → `Cancellations up — ${cancelled7} rooms out this week, vs ${priorCancelled7} the week before.`; also reword "Booking pace is accelerating/slowing" → "Bookings are speeding up / slowing down" (plain-language rule) |
| `586eb1e` | 2026-08-24 | PLAIN-LANGUAGE NARRATION (`cards-v1.9-plain`): user rule — write for a hotel owner, not a revenue expert; short sentences, everyday words, never jargon when a simpler phrase means the same. (1) Card system prompt rule 9 → required rewordings glossary (firming→getting stronger, decelerating→slowing down, compression→filling up fast, ADR dilution→average rate is falling, pickup→new bookings, pace→bookings, OTB→booked so far, lead time→how far ahead guests book, close-in→last-minute, inventory→rooms, rate codes/floors→rate plans/minimum rates, materialise→come through); hero prompt gets the same instruction. (2) New `_PLAIN_TERMS` + `_plainify_text/_plainify_card` — ordered regex substitutions applied AFTER validation to narrated cards, hero, and both fallback paths (en only; sentence-start capital preserved, mid-sentence acronyms lowercased; never touches digits; audit `jargon_replaced` + log line when it fires). (3) Every fallback template rewritten (all 5 signals + softening merge): evidence labels too (PACE→BOOKINGS, ADR OTB→AVG RATE BOOKED, REMAINING OTB→STILL TO COME, PROJECTED→EXPECTED FINISH, CLOSE-IN SHARE→LAST-MINUTE SHARE, SOFTEST→LOWEST DATES, z-score→"swing of X vs normal"). (4) Hero driver hints: rate-led→"mostly from higher rates", occupancy-led→"mostly from more rooms sold", softer→lower/fewer. test_hero.py 53 checks (+26 plain-language), test_leadtime 37, test_retry_feedback 10, test_contract 17 all pass. NOTE: test_preview.py legacy path fails with pre-existing `KeyError: month_num` in `_legacy_generate.pace_row` (not touched; legacy path unused by tunnel hotels). Verify next 03:30 run via cards_audit: `jargon_replaced` should be absent/rare; any hit = tune the prompt glossary |
| `3a57175` | 2026-08-14 | PHASE A LIVE + VERIFIED at `web-cloudflare.up.railway.app`: /health = firstlight-api/phase A; smoke test PASSED — 401 no token, 403 cross-hotel token, /briefing/latest both hotels, /briefing/history 7 days w/ kpi_summary, /feedback (real rows incl. user notes). Root causes of the 4-day-late activation: (1) Railway service had a CUSTOM START COMMAND (`python railway_main.py`) silently overriding the Dockerfile CMD — the Phase A image built but never ran; (2) `$PORT` in the replacement start command didn't expand → 502 crash loop → fixed with `python api.py` entry (port read in-process). Old unauthenticated /trigger is now closed behind tokens. LESSON: after a CMD-level change, verify the SERVED process (/health signature), not just the build. 17:00 UTC run will confirm scheduler+poller in the lifespan |
| (config) | 2026-08-10 | POTIDEA ONBOARDED TUNNEL-DIRECT (first run of docs/ONBOARDING.md, ~10 min from token to verified data): reused the hotel server's existing cloudflared tunnel — added TCP hostname `sql-potidea.hbis.io` → 192.20.10.8:1433 + Service Auth token `railway-potidea-sql`; local pre-flight through db/tunnel.py BEFORE touching prod config (mpehotel=1 confirmed — the only property on its server; zimmer raw 295/filtered 237 vs configured 236, same off-by-one as Pome; NO `mpe` table on this server — sanity by volume instead); pms_config written (BiData, sa for now); first tunnel run: success, 8.3s fetch, complete, no legacy, ALL signal queries populated for the first time (lead_time 96, pickup_daily 42, cancel_daily 27, consumed 8, pace_next_year); yesterday 209rn/€109,138 matches pre-flight to the euro. Intake deferrals (user): rooms stay 236 + season dates pending user's own Protel queries; language en (app-switchable); recipient_email intentionally empty. REMAINING: 03:30 watch → decommission old bridge (daemon + 2 scheduled tasks) → sa→firstlight_ro on BOTH hotels |
| (pending) | 2026-08-04 | CLOSED-SEASON HERO + summary trim + onboarding intake: Q16 `Q_PACE_NEXT` (next-year OTB by month vs this year at same booking stage 364d ago, Q_PACE conventions, fail-open, optional contract field `pace_next_year`); contract carve-out — `yesterday_nonzero` hard gate passes when yd+MTD are all-zero AND next year has bookings (evidence the pipe is alive; all-zero without it still blocks); analyst `_closed_season_slots` → hero pivots to "closed for the season, {ny} has X rn / €Y on the books, {vs stly}, strongest months" (fallback + prompt order both switched, prompt `cards-v1.5-closedseason`); hero chips hidden when yd.revenue=0. Collapsed Smart Summary trimmed to 2 bullets (was 3 — read taller than the full paragraph). ONBOARDING (user-mandated): intake MUST capture real sellable inventory (total_rooms, never the PMS room list) + season open/close dates LY+TY → `hotels.season_settings` (SQL file 2026-08-04, awaiting paste); skill + memory checklists updated. TO-DO: season-aware occupancy denominators (open days, not calendar days) once season_settings is populated |
| `85086f5`+`56c7812` | 2026-08-01 | DYNAMIC PICKUP (verified live): the 4 Pickup Activity boxes are tap targets — selecting Today/Yesterday/3-Day/7-Day filters the booked-vs-cancelled butterfly to ONE bar pair per stay month for that window. charts.py precomputes all 4 windows per month (same counting as the boxes → each reconciles exactly), embeds a JSON payload; ~30 lines of in-page JS swap widths/net/labels/alert (animated, no reload). Selected box = 2px blue border on transparent base (no layout shift) + glow; butterfly title shows the window's calendar range (e.g. "· 27 Jul – 02 Aug") and updates per tap. Server default = 7d. Email keeps static Top-month fallback (widget app-only). NOTE: first wiring left an orphaned `{% endif %}` (regex non-greedy cut) → TemplateSyntaxError caught in local render, fixed before push; the .pw-sel CSS also silently missed its anchor first time — both now assert-checked in render verification |
| `c4b3e46` | 2026-07-26 | Hero paragraph replaces one-sentence executive_summary (prompt cards-v1.3-hero): 4-6 sentence morning narrative — yesterday w/ occupancy-vs-rate driver decomposition, MTD position, top-signal previews w/ at-stake. Posture emerges from ranked cards (alert-led / opportunity-led / steady). One Claude call, numeric+style validator (110-word cap, no imperatives, must start "Good morning."), deterministic fallback, hero entry in cards_audit. Same `executive_summary` field → zero PWA/email changes. test_hero.py = 28 checks |
| `261bffe` | 2026-07-30 | Briefing v9 (verified live): 3 pace charts +26% taller; ALL numbers bold (global groups + highlight_dark filter for hero: € white, +% mint, −% coral on navy); 🔊 narration button on Smart Summary — on-device TTS, waits for async getVoices (voiceschanged + 300ms fallback; first-tap default-male bug fixed), FEMALE English voices only (regex Samantha/Karen/Zira/Aria/...), toggle stop, app-only; 14 ⓘ info buttons — every section + chart card opens a plain-language what-and-why explainer (id-paired panels, inline handlers, exempt from bold rules; becomes the Phase B translations content); Y-axis labels 11px/700; svg font-family enforced via CSS. Upgrade path noted: real TTS (MP3 at morning run, ~$0.01/day) in Phase A if device voices disappoint. INCIDENT AVOIDED: a crashed edit script truncated the template mid-write — restored from git (14416bd), no loss |
| PWA `a1d8291` | 2026-07-30 | PWA repo (Option A, direct): canonical lockup B in top bar + canonical app tile icon.svg + no-cache headers on index/sw so app-shell updates arrive on next cold open (was: device froze old shell indefinitely). Phone icon needs remove/re-add; iOS icon needs a 180px PNG export (SVG apple-touch-icon unsupported) — pending asset |
| `7d6ca37` | 2026-07-30 | LOGO CANONICAL: brand source code committed verbatim (marks 1/2, white app tile #3 = the app icon, lockups A/B/C); gradient tile removed per user; my reconstructed SVGs deleted. Rule enforced: geometry final, never redraw. PWA repo still needs: header → lockup B, icon → tile #3 export set |
| `c6b188e` | 2026-07-30 | Single-header fix: briefing HTML no longer renders an app header (PWA owns it) — was stacking a second header under the PWA chrome. Page starts at tabs + Smart Summary. Verified live |
| `d40b1d0` | 2026-07-30 | APP REDESIGN (design-file specs 12a + 6c + 8b, iterated with user): navy app header w/ new FL sunrise logo (inline SVG: corona rays, gradient chart line, FL letterforms) + wordmark + hotel pill + LAST SYNC line; white centered tab bar; gradient Smart Summary hero (navy 160deg + cyan top-right glow) w/ signal chips Room nights/ADR/Revenue; Manrope everywhere (Outfit + IBM Plex Mono removed, incl. all SVG chart text; 12a's Outfit spec deliberately overridden for consistency); 6c surfaces (page #F1F3F8, borderless 18px cards, navy two-layer shadows, solid separators); heatmap = 8b design w/ BLUE ramp kept (purple declined), occ 13.5px/dates 9.5px, vertical month border; unified label system (captions 10.5/600/#79747E, deltas 700, values 800) enforced by a GLOBAL TYPE RULES block last in cascade (!important groups — add elements to groups, never one-off weights). Top-sources + OTB deltas brought into system. Email shares the template → also gets Manrope/surfaces (flagged, accepted) |
| `19dda3d` | 2026-07-30 | CHARTS + ADR BRIDGE DEPLOYED to the app briefing: briefing/charts.py computes 5 series — meter, velocity 7d/14d, butterfly (real Q14 cancels), demand heat (60d CONTINUOUS calendar, month change = small navy border on the 1st's cell, no divider rows — user spec), and the ADR BRIDGE card (spec reference impl, Decimal, identity-guarded: suppressed if residual > €0.01; 4 floating bars mix=blue/rate=amber + generated narrative sentence + 5-row channel drill). Template renders APP-ONLY (save_preview passes charts; email send() does not). Placements: butterfly REPLACES Top-month + velocity in Pickup; bridge + meter + heat after pace charts. All fail-open: missing series → chart skipped (legacy payloads keep old layout). First real bridge (July, Q15): ADR 593→515, mix −19 / rate −59, rate-dominant, T.O. rate −€44 the top driver |
| `61785a3` | 2026-07-29 | Q15 (consumed_by_source): ADR-bridge input — consumed rn+rev by Sourcen, current month (1st→yesterday) vs LY shifted 364 days (weekday-aligned per spec), active bookings only, logis>0 (comps excluded). Fail-open, optional contract field. test_leadtime → 37 checks |
| `fa22696` | 2026-07-28 | Q14 (cancel_daily): daily cancellations by future stay month, last 14 days — the cancel side of Q9 so gross bookings = net + cancels. Fail-open fetch, optional contract field (never blocks, no legacy_mode). Powers churn-butterfly chart (7d/14d windows) + future cancellation-spike card. test_leadtime.py → 33 checks |
| `be83592` | 2026-07-25 | Signal 3 (booking lead time): Q13 by stay month × source (28d window vs same window LY), fail-open fetch, optional contract field, compute candidates with city/resort bucket profiles (`hotel_type` in pms_config, default resort), max 2 cards/day, tags MONITOR/ALERT/OPPORTUNITY by window direction × pace status. test_leadtime.py = 23 checks |
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

- **ADVISOR PROPOSALS — verdicts from the 2026-09-10 review** (7 mockup
  artifacts built in app chrome; discussed 1-by-1 with the user):
  - §1 three-questions home / 3-card cap: **IGNORED** (user: "ignore it").
  - §2 **"Since Yesterday" strip — BUILT 2026-09-11** (user: "actually
    lets build 2 and 3"; React 4d6607d, SinceYesterday.tsx — client-side
    diff of briefing vs prevB, no backend change). Agreed design: first white card under
    the Morning Brief, max 3 lines, each line attaches movement to a story
    (month / flagged window / open concern) — NEVER restates a pickup
    quantity; quiet fallback line when nothing meaningful moved; pure
    deterministic diff of two stored briefings, no AI cost.
    Mockup: artifact b1aa4bac.
  - §3 **Follow-up engine — BUILT 2026-09-11** (user: "actually lets
    build 2 and 3"; backend 9d3beb4 briefing/followup.py + railway_main
    hook + api.py, React 4d6607d; test_followup.py 20/20; DDL applied to
    Railway PG via tunnel — tunnel fix: id_ed25519 ACL had gone too open,
    ssh IGNORED the key → "channel open failed: unsupported"; icacls
    /inheritance:r + user :R restored it. ⬜ USER MUST PASTE
    docs/sql/2026-09-10_watch_followup.sql in Supabase — feature is OFF
    (schema-tolerant) until then. Both deploys verified live:
    /health build=9d3beb4, bundle C7lQyPIP). Final shape after discussion:
    NO new issues system — the WATCHLIST is the single follow-up engine.
    Month/date-range insights are auto-added to the existing watchlist
    with author chip "FirstLight" (vs "added by you"); states
    Watching · Improving/Worsening derived from the watchlist's existing
    day-by-day history; 2-consecutive-day confirmation before any
    state/direction change; FirstLight items auto-remove on recovery
    (green closing line in FL Pulse) or retire after ~14 stuck days with
    one final note — re-flag as a NEW episode if materially worse OR when
    unchanged gap enters the near-term booking window (urgency = gap ×
    time left); owner items never auto-removed; cap 3–4 FirstLight items;
    cards in FL Pulse appear only on days with news (new/changed/
    resolved) — "cards are news, watchlist is memory"; non-date-shaped
    events (cancel spikes, strong days) stay one-off cards/pushes.
    Event-driven: intraday refreshes update states/rows immediately;
    **user approved moving to 5 data-only refreshes/day**
    (~10:00/13:00/16:00/19:00/22:00 hotel time) — cheap, no AI, thresholds
    unchanged so push volume doesn't grow. Est. 3–4 days build.
    Mockup (final "one watchlist, two authors" version): artifact 79a88a33.
  - BUGFIX same day (React 71dc06e, user: Oct tooltip active after
    switching to Pomegranate): BarPace tip state now clears when the
    months array changes — an open tooltip carried over to the next
    hotel's chart (same class as the DemandHeat sel reset). Verified
    live (CLcQCwwD).
- ✅ 2026-09-11 **ADMIN v1 — usage per hotel & user** (user: "lets build
  the admin, where we also see the usage for each user and hotel";
  backend 08c316d, React 908b1d1). GET /admin/usage (require_admin =
  founder emails via ADMIN_EMAILS env; auth_user now caches the verified
  email; becomes users.is_superadmin under C3) aggregates usage_events
  30d server-side (service role — RLS keeps events write-only for the
  app): per hotel → per user: opens, active days, events, last-seen,
  top-3 event types; emails via GoTrue admin listing. App: "Admin" row
  in Settings visible only to founder emails → AdminSheet. TRACKING
  WIDENED TO ALL USERS (TRACKED_EMAILS null — the designed one-liner;
  history before 11 Sep is demo-only). SCOPE NOTE: client management
  (create account / temporary password / reset / sign-out-everywhere /
  view-as) deliberately waits for C3 own-login — the sheet says so.
  SAME DAY v2 — PORTAL (user: "what is this shit? im speaking for a
  portal where i will have all the accounts, users, usage and
  subscription type"; backend 23082de, React 69281ce): the sheet failed
  in prod (INCIDENT: first-ever browser call to the Railway API — no
  VITE_API_URL in the Pages build AND no CORS middleware; every prior
  feature reads Supabase direct, so the path had never been exercised.
  Fix: hardcoded public API base fallback in api.ts + CORSMiddleware for
  firstlight.hbis.io/pages.dev/localhost; deploy check now tests the
  PREFLIGHT, not just the bundle). Replaced with full-screen AdminPortal
  "Clients": per hotel — plan/status/price/renews pills + INLINE
  subscription editor (PUT /admin/subscription upsert, validated), users
  with last-seen (green <3d) + usage; header totals incl. active €/mo.
  GET /admin/clients = one call. NEW subscriptions table:
  docs/sql/2026-09-11_subscriptions.sql — applied to Railway PG (8 cols
  verified), ⬜ USER PASTES in Supabase (portal shows amber note until
  then). Preflight verified 200 from firstlight.hbis.io.
- ✅ 2026-09-11 **PHASE C READ-FLIP — STORAGE=pg LIVE** (plan step 0,
  user approved + flipped the env in the dashboard; wiring a039f57,
  fix 0f22dc6). Pipeline reads now PG-first with Supabase fail-open
  fallback: hotels listing, yesterday's data/ai_insights (intraday +
  reuse), narration language; intraday claim authority moved to PG.
  Pre-flip verify: 5 tables count-equal, latest briefings MATCH both
  hotels. INCIDENT during flip: first manual run logged "No hotels
  configured" — psycopg returns uuid.UUID/date OBJECTS where Supabase
  REST returned strings, so the hotel-id filter matched nothing; fixed
  by normalizing scalars in every store read (_norm; jsonb stays dict);
  re-queued run: SUCCESS in 10s end-to-end on PG reads. Supabase keeps
  all writes (app + rollback = set STORAGE=dual back). Also: railway CLI
  env-set blocked by permission classifier → env flips are user-dashboard
  actions from now on. 07:20 dual-verify keeps guarding equality.
- ✅ 2026-09-11 **SUPERADMIN PORTAL SHELL** live at
  firstlight.hbis.io/superadmin-control (React 86117af; ADMIN_PLAN
  approved with D1-D3, shell-first option taken): left-nav with all 11
  Phase-1 sections — Overview (platform verdict from /health +
  ClientsView) and Clients LIVE; others stubbed with their build-step
  notes. ClientsView extracted from AdminPortal (in-app overlay reuses
  it + links to the full portal). public/_redirects added (SPA deep
  links). SAME DAY — C3-INDEPENDENT SECTIONS BUILT (backend ac9ec92,
  React 65ffc5e; user: "can we build the sections content now?"):
  §10 Audit log (admin_audit table = migration 001 applied to PG via
  tunnel; store.audit() written by every portal action; list view),
  §2 Hotels (per-hotel: PMS/tunnel/credentials-present/token-present,
  30d ok/degraded/failed + AI cost, expandable last-30-runs table with
  durations, rows, fallbacks, cost; actions: Refresh now via
  refresh_commands, Rotate API token — shown once, 60s cache busted,
  Pause requires a reason), §6 Health (audit_all() verdict reused —
  empty list = "Nothing wrong right now"; 7d day×type pipeline matrix;
  per-card fallback rates 14d from cards_audit; infra: db size, storage
  mode, build), §7 Feedback inbox. PG aggregates in store.py (SQL, not
  REST loops). Still SOON: Users/Security/impersonation (need C3),
  Onboarding, Notifications, Kill switches. NEXT: C3 own login.
  SAME DAY v2 — TABLES (React d838436, user: "no claude cards — excel
  like tables with a filter for each tab"): shared portal table kit
  (src/portal/kit.tsx — sortable sticky headers, filter bar with
  free-text + dropdowns, zebra rows, tabular-nums right-aligned,
  h-scroll wrap); Hotels + Clients = filterable tables with expandable
  detail rows (runs+actions / users+subscription editor); Feedback +
  Audit get per-tab filters; Health verdict = lean strip. LESSON
  (3 broken pushes): `npm run build | tail` MASKS the exit code — the
  pipe returns tail's 0; always check build exit explicitly before
  committing frontend changes.
  SAME DAY v3 (backend 5b2a064, React 0903544; fixes: 65dbd57
  rows_fetched-object render crash, 3658e46 keyed flatMap + error
  boundary + width cap): FINANCE TAB (user: charts for daily Anthropic
  charges, daily data processed, financial reports) — /admin/finance:
  30d daily cost/tokens/rows (PG SQL handles jsonb rows_fetched
  breakdown + legacy numbers); summary strip (cost month/30d, tokens,
  active clients, MRR/ARR), two daily bar charts (Anthropic USD, PMS
  rows), revenue-by-plan + per-client lines + CSV export for invoicing.
  Repeated lesson: the heredoc mixed-quote quirk silently dropped the
  api.ts half of a patch AND `tsc | head` masked its exit — 4 red
  builds; rule: scratchpad scripts only + raw exit codes on every gate.
  SAME DAY v4 — CLIENT REGISTRY (backend 8836f4e, React b26e209/
  Cg10Wc1F; user: company/VAT/contact/hotels/start/rates via an
  onboarding form, feeding Finance; = ADMIN_PLAN D2 executed):
  migration 002 (organizations + legal_name/vat_number w/ per-country
  unique/contact_name/contact_phone; NEW contracts table per company:
  status trial/active/suspended/ended, start_date, monthly_eur,
  annual_eur, billing_anchor, notes — applied to live PG; PG-ONLY, no
  Supabase paste needed); GET/POST /admin/companies (upsert org both
  stores' base row + hotels.org_id to both, VAT dup = 409, audited);
  portal: Onboarding tab = the new-client form, Clients tab = company
  table (Company|VAT|Contact|Hotels|Status|Start|€/mo|€/yr|Users|
  activity) w/ expandable users + inline edit; Finance revenue reads
  contracts (annual/12 for yearly), legacy hotel subscriptions only as
  fallback → the 2026-09-11_subscriptions.sql Supabase paste is now
  OBSOLETE (never required). Verified: gate 401, form live.
  SAME DAY v5 — GROUP LAYER (backend af05355, React 0b885ea/BbyChefq;
  user: hierarchy = Group e.g. Myconian Collection → 3-4 companies →
  14 hotels): migration 003 (groups + organizations.group_id, lifted
  idempotent from the C3 tenancy draft, applied to live PG);
  GET/POST /admin/groups; onboarding form leads with Group (pick or
  create inline) → company → hotel assignment; Clients table adds
  Group column + filter. C3's tenancy apply stays compatible
  (if-not-exists on the same DDL). Verified live.
- ✅ 2026-09-11 **PG SECURITY HARDENING — least-privilege roles LIVE**
  (0fde7a6; user approved the "three keys, no overkill" plan after the
  external PostgreSQL security guide review). fl_app = worker
  (data-only, no DDL, statement_timeout 30s / lock 5s / idle-in-tx 60s
  set server-side), fl_readonly = visitor (default read-only, hotels
  secret columns invisible via column grants, connlimit 5), postgres =
  builder (tunnel-only migrations). docs/sql/pg/grants.sql idempotent
  incl. ALTER DEFAULT PRIVILEGES (future tables covered) — re-apply
  after every migration. store enforces sslmode=require in the DSN
  (server handshake verified). USER flipped DATABASE_URL to fl_app in
  the dashboard (CLI env-set stays classifier-blocked). Verified end to
  end: 9/9 role checks (incl. must-fail cases), then a full manual
  refresh ran SUCCESS in 36s on the worker key (health 0fde7a6).
  Passwords in C:\FirstLightBackups\pg.env. docs/SECURITY_CHECKS.md =
  quarterly 10-min self-check + deferred controls with triggers
  (pgaudit/RLS/CI scanners/scrubbed restores/KMS). Superadmin UX
  unchanged — DB roles are plumbing beneath the API.
- ✅ 2026-09-11 **CAPS v1.9.2** (604af18, user "go"; live-verified
  prompt_version cards-v1.9.2-caps): what_happened 28→33, why 42→46,
  action 32→36, hero 125→132 — sized to the OBSERVED overshoots
  (cards_audit: 29-32/33 words), which had Pome+Potidea shipping
  fallback cards near-daily (~30/30d). No-retries policy unchanged.
  test_hero over-length fixture now cap-relative (was pinned to 125 —
  it silently passed the new cap; fixed the test, not the pin).
  ⬜ VERIFY tomorrow's 03:30: both hotels success (not degraded);
  Health tab fallback rates should fall from 2026-09-12 on.
- 🔄 2026-09-11 **C2 APP REPOINT — READS SHIPPED** (backend 6753632,
  React 0b747c3; user: "can we go for c2 repoint"). NEW /app/* data
  plane: hotels, briefing latest/by-date/prev/dates/history, runs —
  user JWT + hotel membership, PG-FIRST server-side (new store fns
  get_briefing_dates / get_recent_briefings / get_prev_briefing) with
  Supabase fail-open fallback. App fetchers (briefings, dates, history,
  runs, hotels list, watchlist read/add/remove) now call the API first
  and keep their Supabase-direct code as AUTOMATIC fallback = the
  runbook's parallel-run built in; demo mode untouched; prev-briefing
  composes from dates+by-date so it repoints for free.
  SAME DAY — (b)+(c) SHIPPED (user: "lets switch everything to
  postgresql"; backend f3e0c7f, React 8ea119c; perf fix 8cfebcc first:
  gzip >=2KB + 5-min membership cache after the user felt 1-2s on
  history): **PG IS NOW AUTHORITY for every app table** — watchlist
  (insert returns PG id, echoed to Supabase with the SAME id; cap+dup
  server-side), insight_feedback, hotel_prefs, push_subscriptions
  (+ NEW GET /push/status replacing the app's direct reads),
  usage_events (batch) — all endpoints write PG-primary via store with
  best-effort Supabase echo; app write paths (feedback, language,
  push subscribe/unsubscribe/prefs, event batches) now call the API
  first with Supabase-direct as automatic fallback; followup engine
  watchlist IO moved to store(PG)+echo; admin usage/feedback reads
  PG-first. REMAINING ON SUPABASE: auth (GoTrue JWT verify) +
  hotel_users membership — both die with C3. NEXT: a few clean days →
  drop the echoes + nightly mirror → C3 → 14-day Supabase freeze →
  account deleted. Dual-verify keeps watching both stores meanwhile.
- ✅ 2026-09-11 **EXTERNAL SECURITY PROBE + HEADERS** (0b9d59a, user:
  "check any vulnerability or risk with the url, product, data").
  PROBED FROM OUTSIDE: all API gates reject unauth (admin/app/watchlist/
  push/trigger — two return 422-before-401, cosmetic, nothing executes);
  the Supabase anon key was EXTRACTED FROM THE LIVE BUNDLE and tried
  against briefings/hotels(api_token!)/watchlist/refresh_runs/
  usage_events — ALL EMPTY, RLS holds. GAP FOUND: no HSTS/CSP/
  frame-protection on the app → web/public/_headers added (HSTS 1y,
  CSP self-only scripts + connect-src limited to our Supabase+API,
  X-Frame-Options DENY, Permissions-Policy lockdown) — live-verified,
  bundle still serves. ⬜ user to click through the app once (CSP is a
  whitelist; human smoke test). YELLOW register: localStorage sessions
  (CSP-mitigated, C3 fixes), plaintext hotel api_tokens (C3 hashes),
  /health names stale hotels publicly (trim to count by launch),
  transition echoes to Supabase (time-boxed). TOP REMAINING RISK is
  hotel-side `sa` (user's firstlight_ro item).
- ✅ 2026-09-11 **PER-ENTITY AI TOGGLE** (backend 00a6e0f, React
  7917acd; user: toggle per company/group/hotel so it won't spend
  tokens). Migration 004 (ai_enabled nullable on hotels/orgs/groups,
  applied to PG); effective flag = coalesce(hotel, org, group, true);
  generate_insights(narrate=False) → deterministic fallback cards +
  no hero, ZERO tokens, briefing still publishes (cards_audit marks
  ai_disabled_for_entity); pipeline FAILS OPEN to narrating on lookup
  error; PUT /admin/ai-toggle (audited; gate verified 401 w/ junk
  bearer); portal: Hotels AI column + Turn-AI-off/on action, company
  form 3-way select. Verified live (CNe8rPXC). Suggested first use:
  demo hotels under an "HBIS Demo" company toggled off.
- 📌 2026-09-11 **OPERA ON-PREM = PRE-FREEZE TRACK** (user: real
  multiproperty Opera hotel available, validatable against Hotel BI's
  own numbers — which also means HBIS already possesses working SQL
  against that exact Opera DB). Plan: C3 stays primary; I write
  SEMANTICS.md + opera_oracle adapter skeleton + validation harness
  (diff vs Hotel BI) meanwhile; USER kicks off now (lead-time items):
  ① send Hotel BI's Opera queries/views, ② Opera+Oracle versions and
  which machine can host cloudflared, ③ read-only Oracle account
  (firstlight_ro from day one), ④ resort/property codes, ⑤ real rooms
  + season dates per property. Target: first Opera briefings early Oct;
  multiproperty feeds the portfolio view. Booth line: live Opera group
  validated against its own BI.
  - ✅ 2026-09-11 **Opera PRE-FLIGHT DONE (direct over VPN from the dev
    laptop, `py -3.13` + `oracledb` thin 3.x, before any tunnel):**
    server `192.168.0.156:1521/opera`, Oracle **19c EE 19.21**, SID
    `OPERA` (services `OracleServiceOPERA` +
    `OracleOraDB19Home1TNSListener`; registry home `KEY_OraDB19Home1`,
    `USE_SHARED_SOCKET` NOT set = default FALSE → listener redirect risk,
    decide only if the tunnel pre-flight hangs; Opera WebLogic runs on the
    same box so any restart = front-office downtime). Read-only account
    provided by the hotel: **`opera_ro`** (CREATE SESSION +
    `OPERA_RO_ROLE`; no dictionary access — `v$database` 942, fine).
    Schemas: **`OPERA` = live (2,185 tables, 149,912 reservation_name
    rows)**, `TRAINING` = copy (7,454) — adapter MUST hard-code owner
    `OPERA`. Opera objects are VIEWs over `*_E` tables (query the views).
    **Resorts:** `CITY` City Hotel Thessaloniki (192 rooms in `room`,
    261 in-house RN yesterday), `EXCEL` The Excelsior (108 / 74),
    `ONRES` ON Residence (127 / 101); `CRO` + `ORS` = central-res /
    demo, exclude. Statuses seen: RESERVED, CHECKED IN, CHECKED OUT,
    CANCELLED, NO SHOW, PROSPECT; history from 2024-02, OTB to 2027-11.
    Business date lives in view `OPERA.BUSINESSDATE`. All three are city
    hotels (no season). Tunnel plan: ONE hostname `sql-<group>.hbis.io`
    → `192.168.0.156:1521`, one token, three `hotels` rows (PG) keyed by
    resort code. Room counts above are PMS room lists — intake still
    needs the REAL sellable inventory per property.
  - ✅ 2026-09-11 **TUNNEL LIVE — `sql-torcity.hbis.io` →
    `192.168.0.156:1521`** (tunnel + hostname + Service Auth app set up
    by the user in the dashboard; service token Client ID
    `0c63dc1b4de87eb79644ab92ba2a6c0f.access`, secret goes ONLY into
    `pms_config`). Pre-flight from the laptop via `cloudflared access
    tcp` on 14331: Oracle connect through the tunnel **0.4 s**, query
    0.1 s, in-house counts identical to the direct-VPN probe →
    **listener redirect is NOT an issue, `USE_SHARED_SOCKET` stays
    untouched, no Oracle restart needed.** Browser hit on the hostname
    without the token → 403 (Access enforcing). Hotel side is DONE.
    Note: `OPERA.BUSINESSDATE` is a per-day calendar, not "current
    business date" — adapter needs another source (TO-DO in adapter
    work). NEXT: three `hotels` rows in PG (CITY/EXCEL/ONRES, shared
    tunnel block, `pms_type` opera) + oracledb in requirements +
    `db/adapters/opera_oracle/` from Hotel BI's queries.
  - ✅ 2026-09-12 **ADAPTER BUILT + VALIDATED** (see release history
    2026-09-12 + decision log). Hotel BI's queries received (reservations
    table = the WBLKNM view + RESGENERAL join; room types =
    `RESORT$_ROOM_CATEGORY.NUMBER_ROOMS`, pseudo flag). Physical rooms
    from that model: **CITY 125, EXCEL 36, ONRES 60** (room view counts
    192/108/127 include 51 PM + PI + catering posting masters each).
    Timing mystery solved: the 90 s extract was the pseudo predicate in
    WHERE, not server load. REMAINING before first Opera briefing:
    ① user confirms real sellable rooms + VAT/company + users, ② three
    `hotels` rows in PG (`pms_type: opera_oracle`, shared tunnel block,
    `sql.pms_hotel_id` = resort code), ③ push → Railway build with
    `oracledb`, ④ manual refresh per property → `refresh_runs` with
    `fetch_path: tunnel`, ⑤ 03:30 watch. Open question for the user:
    Hotel BI prices cancelled nights at the daily rate, we use Opera's
    `CLX_ROOM_REVENUE_GROSS` (0.1–0.2% apart on STLY) — keep ours unless
    told otherwise.
  - §4–§7 (show-me-why, feedback reasons, quiet day, Greek voice):
    discussion pending, mockups live (artifacts c2e739de / deca2a7c /
    36bfdb29 / fd30d282).

- **USER IMPERSONATION ("Log in as user", added 2026-09-10, user request)**:
  a superadmin (dk@) can open any real client's account and see exactly what
  that user sees — for support, demos, and debugging ("why does my chart look
  wrong?"). Standard SaaS feature, a.k.a. impersonation / "view as".
  Design (build AFTER the own-login tenancy work lands, it makes this easy):
  `users.is_superadmin` flag → `POST /admin/impersonate {user_id}` (superadmin
  token required) issues a SHORT-LIVED session for the target user carrying an
  `impersonated_by` claim → app shows a persistent banner "Viewing as {name} —
  exit" and blocks destructive actions (sign-out, password change, prefs
  writes optional). EVERY impersonation start/stop goes to an audit table
  (who, whom, when, from where) — non-negotiable once real clients are on.
  Analytics/usage tracking must EXCLUDE impersonated sessions. Effort: small
  (~half a day) once our own auth is live; do NOT build on Supabase auth.
- PWA update to render the new card anatomy (BY WHEN box, tappable AT STAKE calc,
  evidence labels) — backend already ships the fields
- PWA: language toggle — Greek / English (per-user preference; affects briefing
  narration too, so backend prompt needs a language parameter)
- PWA: text-size setting — whole-report scale selector, levels 1–5, like phone
  accessibility font sizing (requested 2026-07-24)
- PWA: the 3 OTB charts are too small — enlarge charts and axis/data labels
- Charts (Revenue OTB + Occupancy): for months fully in the past, STLY and
  Final LY are the same number — show ONE LY indicator (Final LY) for closed
  months; keep both only for current/future months (requested 2026-07-26).
  Note: chart is generated in OUR templates (rendered_html), so this is
  fixable backend-side without touching the PWA
- PWA multiproperty bug: switching hotel jumps straight to AI insights section —
  should reset scroll to top of the report (requested 2026-07-24)
- Mobile chart library (design handoff received 2026-07-27; test gallery with
  REAL Pome data built same day for keep/adjust/skip decision).
  CHART 1 SPEC DECIDED 2026-07-28 (curve position meter, under OTB charts):
  app design language (not handoff tokens); ALL values in ROOM NIGHTS, no pts;
  per month row — grey track = LY final (end labeled), bar = booked now,
  tick = LY same date (labeled inline); right column = "+X rn vs LY pace" +
  "Y rn to reach LY final". Colors: bar BLUE always; RED only when behind LY
  same-date pace; GREEN only as overflow segment past track end when booked >
  LY final (labeled "+Z rn above LY final").
  CHART 2 SPEC DECIDED 2026-07-28 (velocity / booking speed, under Pickup):
  net rooms/day (bookings − cancellations, from pickup_daily), TWO bars per
  month — last 7d (navy) + last 14d (accent blue) — for CURRENT + NEXT 3 stay
  months; grey tick = LY speed same time (lead_time 28d window); amber tick +
  "need X/day" = gap to LY final ÷ days left, REPLACED by green "✓ passed LY
  final (+rn)" once booked ≥ LY final; right column shows both speeds labeled
  "/day · 7d|14d" + speeding up / slowing down / steady (7d vs 14d).
  CHART 3 SPEC 2026-07-28 (churn butterfly, REPLACES Top month in Pickup):
  butterfly kept — cancelled left (red tones) / booked right (blue tones),
  arms split into THIN PAIRS: top = last 7d, bottom = last 14d, 4-swatch
  legend on top; net per window right (red if cancels >60% of gross); amber
  alert names worst-churn month. REAL data since Q14 live (2026-07-29 run:
  Aug 294 cancels/850 booked in 14d = 35% churn — sample had guessed 45).
  CHART 5 SPEC DECIDED 2026-07-28 (demand heat, under OTB): next 60 DAYS,
  calendar grid 7 weekday cols (M-S, date-aligned), bigger cells each showing
  OCCUPANCY % on top + date dd/mm below; shade = occupancy (7-step blue ramp,
  what you read is what colours it); red outline + amber alert = date far
  behind LY (occ < 50% of LY when LY ≥30%). Real find: 23-25/09 flagged.
  CHART 4 (sparklines) comments pending.
  NEXT FEATURE ACCEPTED 2026-07-29: ADR BRIDGE (mix vs rate decomposition,
  spec + reference impl received as docs — identity-guaranteed midpoint
  method, consumed periods only, 364-day shift, Decimal, centering on ADR-bar,
  min_share 3% fold to Other). Needs Q15: per-channel consumed room nights +
  revenue, July TY vs July LY(364d) — channel split not in payload today.
  Narrative from structured payload only (mix-dominant / rate-dominant /
  both templates, no imperatives). Second dimension room_type after channel.
  User placements:
  1 Curve position meter → under OTB charts · 2 Velocity bullet → under Pickup ·
  3 Churn butterfly → REPLACES "Top month" in Pickup · 4 Sparkline multiples →
  Pickup (trend) · 5 Demand heat strip → under OTB (demand dates).
  Data readiness: charts 1/2/4/5 run on data already shipped; chart 3 needs
  Q14 (cancellations by stay month, 28d window — ~half day incl. contract);
  chart 4 full fidelity wants Q9 widened 14→30 days. Implementation = Phase B
  React components per handoff tokens (44px rows, 14px marks, no chart lib,
  Outfit + IBM Plex Mono).
- PWA: ⓘ info button on EVERY section (requested 2026-07-27) — tap opens a
  tooltip/sheet explaining what the section shows and how to read it (e.g.
  Pace: "rooms on the books per month vs the same point last year; Final LY =
  where the month actually ended"). Copy written per section, kept in a
  translations file from day one so the Greek toggle covers it; definitions
  should match db/adapters/SEMANTICS.md wording so app language = metric truth.
  Phase B scope.
- 7-day history with day-over-day KPI deltas (requested 2026-07-24). Design:
  NO new PMS queries — deltas come from stored daily snapshots (briefings has one
  row per hotel per report_date already). Backend: (a) new `briefings.kpi_summary`
  jsonb column (~200B: occupancy_today, rooms_otb, revenue_mtd, adr, pickup_7d),
  populated by cloud_push at publish + SQL migration w/ index (hotel_id,
  report_date); (b) `GET /briefing/history?hotel_id&days=7` returning slim rows
  ONLY (never rendered_html — large-row lesson); PWA computes ▲▼ deltas
  client-side. Phase 2: tap a day → full past briefing via `GET
  /briefing/by-date` — depends on PWA render-from-data (avoids storing old HTML).
  Manual refreshes overwrite the day's row → history shows final state per day;
  failed mornings show as gaps.
- PWA: render briefings from `data`/`ai_insights` JSON instead of `rendered_html` —
  PREREQUISITE for removing HTML storage (see 2026-07-23 incident); also permanently
  fixes the large-row 500 on `/briefing/latest`
- Potidea `/briefing/latest` 500 (see incidents)
- ~~Signal 3 (lead-time/booking-window signal)~~ SHIPPED 2026-07-25 (see release
  history) — lead-time is a first-class metric: demand timing shifts by location
  (per-hotel LY baseline), period (per stay month), and source (per-channel
  drill-down). City vs resort bucket profiles via `pms_config.hotel_type`
- Follow-up loop (track advice given → outcomes; N component of score) — Phase 2,
  builds on refresh_runs card audit
- Chatbot agent on briefing data; monetization tiers — discussed, parked
