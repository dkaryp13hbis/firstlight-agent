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
- REVIEW 2026-10-28 (`cards-v1.11-review`, ENGINEERING_LOG §3): every signal is
  its own function `_signal_<name>(ctx)` registered in `_SIGNALS` and run
  fail-open by `_compute_signals` (errors → cards_audit `compute_errors`).
  `ctx["today"]` comes from `report_date_iso` (`_anchor_today`) — never call
  `date.today()` in a signal. Monthly signals iterate `ctx["months"]` (this
  year from today + next year within 183 days, each row has `year`; use
  `_month_row(ctx, m, y)`), never `pace` by month number. Q17 has no year:
  never attribute next-year months.
- MIXED (`pace_mix_*`): rooms ≥ +10% but revenue ≤ −€1,000 vs same time last
  year → ALERT with rooms % / average-rate % / revenue % (they reconcile) and
  `_mix_rate_split` (charts.py ADR bridge on Q17, only when Q17 ≈ Q4 within 2%).
- HIDDEN SOURCE (`hidden_src_*`): month within ±10% overall, one source ≥ 20%
  and ≥ 10 rn behind (≥ 20 rn last year, stake ≥ €1,000), other sources
  together ahead, Q17 totals ≈ Q4; max 2/day.
- Lead time: `_lead_shift_attribution` names a source only at ≥ 60% of the
  shift (exact signed split). Shares above 100% read "more than the whole
  gap/change, partly offset by other sources" (`_share_of`).
- Soft/hot dates compare the same CALENDAR date last year — never claim a
  weekday pattern from them (needs Release 2 same-weekday data).
- SIZE IS NOT A CAUSE: a slice explains a gap only when over-represented in it
  (≥ 60% of the gap AND ≥ 20 pts above its share of volume).
- FL Pulse audit fixes (2026-10-28): pickup ALERT attribution has THREE outcomes
  (cancellations / new bookings / both) from the exact split swing = drop in
  new bookings before cancellations + extra cancellations, and says nothing
  when Q9 and Q14 disagree; a pace card whose top source moved > 125% of the
  month's net tells the OFFSET story ("TEL alone is −236 rn; other sources
  together +178"); cancellation spikes take C from the spike (z ≥ 3 → 1.0).
- NOVELTY FLOORS: `_fingerprint` returns `floor` (lead time 1 day, % metrics
  3 pts, room nights 5 rn) — a repeat is news only at ≥ 10% AND ≥ floor; a
  source-named pace card fingerprints that source's move; a date set needs
  ≥ half new dates. New card types must set a floor.
- CONFIDENCE: "High" only for measured facts — never on likely/may wording.
  `_confidence_violations` rejects a narrated label above Layer A's (lower is
  fine); fallback templates must carry the same label as their hypothesis.
- CROSS-FILTER ENGINE (`briefing/discovery.py`, Release 2, DARK): raw numbers
  only (no formatting, no Claude); `_discovery_candidate` in analyst.py does the
  presentation. `DISCOVERY_CARDS=1` lets discoveries compete — otherwise they
  are cards_audit `discovery:<id>` WATCH entries only. A new dimension = one
  `_DIMS` entry + data in Q18; the gates (`explain`) stay as they are.
- Scoring `_score_candidate(R,U,M,N,C)` = (0.35R + 0.25U + 0.25M + 0.15N)·C;
  ranked = score ≥ 0.08, top 6, narrate top 5.
- Merges before ranking: same-month pace+projection; pickup ALERT + soft dates.
- DATE ENGINE (2026-10-31, DARK unless `DATE_CARDS=1`): `briefing/date_events.py`
  finds surge / cluster / ly_event / week on the next 90 nights vs the SAME
  WEEKDAY last year (Q18 cube, Q21 for surges); `_date_candidate` words them,
  signal `date_event`, fingerprint = nights + rn move (surge = event key).
  Dark findings go to cards_audit `dates:<id>` WATCH. Runs > 7 nights are
  month stories; zero-booked nights are never 'behind' (closed / unknown).
  `fill` (fast-filling nights, 2026-10-31): last-7-days net (Q21) >= 3 rn,
  >= 8% of rooms and >= 3× the same weekdays nearby, >= 7 days out, not sold
  out, run >= 8 rn, < 70% from yesterday (else surge), and the 7-day speed
  would book the free rooms before arrival — a GATE, never a shown
  projection. Fingerprint metric = rooms booked on those nights. It replaces
  an overlapping ahead cluster / upward surge (one story per night).
- ONE EXPLANATION PER CARD (2026-10-31): a pace card hands narration ONE
  driver — the source, unless the segment's share is >= 15 pts higher; the
  other goes to the candidate's `drill_extra` (drill-down only, never sent to
  Claude). Two drivers made Claude overrun the caps (5 / 15 fallbacks).
- CARD WINDOW (2026-10-30, user): month cards cover THIS month while >= 7
  nights are left + the next 4 calendar months across the year end
  (`_in_card_window`, `_CARD_MONTHS_AHEAD`, `_MIN_NIGHTS_LEFT`; applied to
  every candidate after the signals and to discoveries). Soft / hot DATE
  cards are exempt. A new month-level signal needs a correct
  `insight.stay_period.from` or the window cannot place it (kept, never
  dropped on a guess). `since_first_flagged`: signed metric only, 'lead'
  when ahead, never on event (`day:`) cards.
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
  expanded or explained (Claude turned "BK" into "Booking.com", and on
  2026-10-30 "TEL" into "telephone"). User rule: codes stay codes — no
  glossary. `_source_violations` rejects brands AND generic channel words
  (`_CHANNEL_WORDS`) that are not in the input.
- Source rows (`sources_by_month`) carry `stay_year` (Q17 this year + Q20 next
  year): always read them through `_q17_rows(sbm, month, year)`.
- Pickup ALERT / OPPORTUNITY name the source + stay dates behind the swing via
  `_pickup_source_attribution` (Q21 `pickup_by_source`), same gates as the
  other attributions.
- DAYTIME PUSHES REUSE TWO SIGNALS (2026-10-31, `briefing/intraday.py`
  `pulse_checks`): `_signal_pickup` (OPPORTUNITY only → "good news") and
  `_signal_cancellations` (→ "alert") run on today-so-far by re-anchoring
  `report_date_iso` to today. The push text reads their facts keys
  (`month_label`, `yday_net_rn`, `trailing_avg`, `yday_net_rev`,
  `cancelled_yday`, `cancel_avg_prior`, `cancelled_rev_yday`) — renaming one
  breaks the push text; test_intraday.py pins the exact wording.

## Facts contract (validator enforces)

- Every displayed number formatted by helpers (`_eur`, `_pct`, `_pts_signed`,
  `_rn_signed`, `_fact(value, period)`) — facts are `{"value","period"}` dicts
  so narration can't blend periods (`_period_violations`).
- Numeric validator: any number in card output must appear VERBATIM in the
  narration input (`_bad_numbers`). Compute exact display strings in Layer A.
- WORD-LIMIT CONTRACT (canonical: `_WORD_CAPS` + `_HERO_WORD_CAP`; table in
  ENGINEERING_LOG §3): headline 12 / what 33 / why 46 / action 36 / by_when 10
  / hero 132 (relaxed 2026-09-05 and 2026-09-11 by the user — do not tighten
  without a fresh yes). Enforced on EVERY path: schema descriptions (~80% targets) →
  validator (SINGLE attempt, cost policy — `NARRATION_ATTEMPTS` re-enables
  retries) → fallback templates test-proven within caps (any new fallback
  template MUST add the cap check to its test) → `_enforce_caps()` runtime
  clamp last resort (a CAP CLAMP log line = template bug, fix the template).
- Soft language, no imperative openers (`_BANNED_IMPERATIVES`).
- REVIEW / INVESTIGATE ONLY, enforced in code (2026-10-28): `_action_violations`
  (`_COMMERCIAL_ANY` on every field + hero, `_COMMERCIAL_ACTION` on action /
  by_when) rejects commercial advice anywhere in a sentence ("consider a
  targeted offer", "room for a higher rate", "lowering rates"). Fallbacks and
  directive names (`review_rates`, `investigate_gap`, `monitor_timing`,
  `investigate_dates`, `investigate_source`, `investigate_mix`) must pass it —
  test with `template_ok` in test_review_r1.py.
- Card shape (`_shape_problems`) and source fidelity (`_source_violations`:
  no channel brand that is not in the input) reject → that card's fallback.
- `_number_watch` (sign / € / unit vs input) is WATCH-ONLY → cards_audit
  `number_watch`; `_bad_numbers` is still the enforcing number check.
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
`_driver_hint`) → `_narrate_hero` (previews top cards — each marked `new` or
`still open`, leads with the newest finding, never an at-stake € — must start
"Good morning.", ≤ 132 words) → `_hero_fallback` deterministic. Audit entry
`card_id: "hero"` in cards_audit.

## Checklist: new signal → card

1. A `_signal_<name>(ctx) -> list[dict]` function added to `_SIGNALS` (copy
   `_signal_hidden_source`): gates → facts (formatted, period-scoped) →
   hypotheses/directive (review language) → fallback_card (2 evidence rows) →
   score → candidate dict with unique `insight.id` (novelty gate keys on it),
   `month_num` + `stay_year`, and a `_fingerprint` branch.
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
