---
name: analyst-signals
description: Adding or tuning analyst signals, insight cards, or the hero paragraph — the compute/narration split, hard gates, scoring, validators, and the checklist for a new signal.
---

# Analyst (briefing/analyst.py)

Two layers, strictly separated:
- **Layer A — compute** (`_compute_signals`): pure Python. Detects, gates,
  scores, merges, ranks. Produces per-candidate `insight` (facts) +
  `fallback_card`. No Claude here.
- **Layer B — narration**: one Claude call per card (`_narrate_card`) + one for
  the hero (`_narrate_hero`). Claude words things; it never decides what
  matters. Model `claude-sonnet-4-6`, prompt version `_PROMPT_VERSION`.

## Hard gates (spec, do not soften)

- Pickup fires only at |z| ≥ 2 (7-day trailing). Other signals need ≥ 10%
  deviation. Everything needs stake ≥ `_STAKE_FLOOR_EUR` (€1,000).
- Lead time also needs ≥ 15 rn in BOTH periods AND a shift of ≥ 2 days
  (`_LEAD_SHIFT_MIN_DAYS`, 2026-09-28: 10d vs 9d cleared 10%); max 2 lead-time cards/day.
- Cancellation spike (Signal 6, Q14 `cancel_daily`): yesterday's cancels for a
  stay month vs the prior 7 days ZERO-FILLED (Q14 only ships days with
  cancels), z ≥ 2, ≥ 3 rn, stake = REAL revenue of the cancelled stays
  ≥ €1,000; max 2/day; folds into a same-month pickup ALERT
  (`_merge_cancel_into_pickup`) — never two cards for one story.
- ATTRIBUTION (2026-09-23): cards answer the question they used to ask.
  Pickup ALERT carries `cancellations_yday` / `cancellations_avg` and says
  "mostly cancellations" (extra cancels ≥ ½ the swing) or "new bookings
  slowed" (Medium). Pace cards carry `biggest_source_move` /
  `biggest_source_share` / `direct_bookings_move` from Q17
  `sources_by_month` (`_source_attribution`): concentrated ≥ 60% in one
  source → source-specific (Medium); broad < 40% across 3+ → weaker demand
  overall (Medium); ONLY when the source deltas reconcile with the pace gap
  (±20%) — otherwise say nothing. Both are fail-open: no Q14/Q17 → the
  original Low hypotheses ship unchanged.
- Scoring `_score_candidate(R,U,M,N,C)` = (0.35R + 0.25U + 0.25M + 0.15N)·C;
  ranked = score ≥ 0.08, top 6, narrate top 5.
- Merges before ranking: same-month pace+projection; pickup ALERT + soft dates.
- NOVELTY v2 (2026-09-24, "cards are news, the watchlist is memory"): every
  candidate carries a FINGERPRINT (`_fingerprint`: metric = rn_gap / vs_ref
  / lead_shift / stake; key = date set for hot & soft dates, event day for
  pickup/cancellation/softening; days_out) stored on the insight as
  `_novelty`. `_novelty_decide` (pure, test_novelty.py): a card seen in the
  prior 7 days ships again ONLY when a key token is new, its metric moved
  ≥ 10% vs the LAST sighting (worse or better), or it entered the last 30
  days. NO 3-card floor, NO scheduled resurfacing. QUIET-DAY PIN (2026-09-28,
  `_pin_open_alert`): with < `_QUIET_DAY_CARDS` (2) fresh cards, the single
  highest-scoring demoted ALERT (score ≥ 0.08) ships as ONE "Still open" card
  (facts.status "still open, first flagged N days ago"); busy days never
  re-ship a repeat. Otherwise a quiet day is hero-only with a pulse note
  (`slots["pulse"]` → "Nothing new in the Pulse today; 3 items still open").
  Payload carries `open_items` (+ `first_flagged_iso`) + `quiet_day`; the PWA
  renders open_items as the "Still open" list under the cards. `_first_seen`
  on shipped cards / `first_flagged_iso` on open_items carry the TRUE first
  sighting forward, so first_flagged never drifts with the 7-day window. Compares against PREVIOUS
  report_date only, never same-day. New card types MUST get a fingerprint
  branch (or they fall back to stake, and stake-less ones read as repeats).
- Narration rule 15: booking SOURCE codes are copied verbatim, never
  expanded (Claude once turned "BK" into "Booking.com").

## Facts contract (validator enforces)

- Every displayed number formatted by helpers (`_eur`, `_pct`, `_pts_signed`,
  `_rn_signed`, `_fact(value, period)`) — facts are `{"value","period"}` dicts
  so narration can't blend periods (`_period_violations`).
- Numeric validator: any number in card output must appear VERBATIM in the
  narration input (`_bad_numbers`). Compute exact display strings in Layer A.
- WORD-LIMIT CONTRACT (canonical: `_WORD_CAPS` + `_HERO_WORD_CAP`; table in
  ENGINEERING_LOG §3): headline 12 / what 20 / why 35 / action 25 / by_when 10
  / hero 110. Enforced on EVERY path: schema descriptions (~80% targets) →
  validator (SINGLE attempt, cost policy — `NARRATION_ATTEMPTS` re-enables
  retries) → fallback templates test-proven within caps (any new fallback
  template MUST add the cap check to its test) → `_enforce_caps()` runtime
  clamp last resort (a CAP CLAMP log line = template bug, fix the template).
- Soft language, no imperative openers (`_BANNED_IMPERATIVES`).
- PLAIN LANGUAGE (user rule 2026-08-24): the reader is a hotel owner, not a
  revenue manager. Short sentences, everyday words, no jargon when a simpler
  phrase exists (pickup → new bookings, pace → bookings vs last year, OTB →
  booked so far, ADR → average rate, close-in → last-minute, compression →
  filling up fast, soft dates → low-booking dates). Prompt glossary (rule 9)
  + `_PLAIN_TERMS`/`_plainify_text()` substitution pass on narrated AND
  fallback text. Any NEW fallback template must be written in plain words
  (test: `_plainify_text(template)` returns no hits — see test_hero.py).
  A `jargon_replaced` entry in cards_audit means the prompt slipped — add
  the phrase to the rule-9 glossary, not just to `_PLAIN_TERMS`.
- Projections only as bands (occ ±2pts `_occ_band`, rev ±2% `_rev_band`) —
  never a point estimate.

## Hero (top of app, `executive_summary` field)

`_build_hero_slots` (yesterday + MTD with occupancy-vs-rate driver via
`_driver_hint`) → `_narrate_hero` (previews top cards, at-stake only for the
lead story, must start "Good morning.", ≤ 110 words) → `_hero_fallback`
deterministic. Audit entry `card_id: "hero"` in cards_audit.

## Checklist: new signal → card

1. Layer A block in `_compute_signals` (copy the Signal 3 `lead_time` block):
   gates → facts (formatted, period-scoped) → hypotheses/directive (soft) →
   fallback_card (2 evidence rows) → score → candidate dict with unique
   `insight.id` (novelty gate keys on it).
2. Tags: ALERT / OPPORTUNITY / MONITOR only (schema-enforced).
3. Cap cards per signal if it can fire for many months.
4. No prompt changes needed — narration is generic over the insight wrapper.
5. Test script `test_<signal>.py`: gate boundaries (below-gate, low-volume,
   stake floor), fact formatting, fallback shape. Run `py -3.13`.
6. Bump `_PROMPT_VERSION` only when narration prompts/validators change.
7. Verify in production next morning via `cards_audit` (attempts,
   validation_problems, fallback_used) — see `ops-monitoring`.

## Cost guardrails

Per-card calls kept deliberately (validator isolation). ~$0.03–0.05/hotel/day
at 4–5 cards + hero. `CLAUDE_CONCURRENCY` semaphore caps parallel calls.
