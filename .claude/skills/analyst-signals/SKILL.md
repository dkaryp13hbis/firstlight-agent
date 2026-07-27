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
- Lead time also needs ≥ 15 rn in BOTH periods; max 2 lead-time cards/day.
- Scoring `_score_candidate(R,U,M,N,C)` = (0.35R + 0.25U + 0.25M + 0.15N)·C;
  ranked = score ≥ 0.08, top 6, narrate top 5.
- Merges before ranking: same-month pace+projection; pickup ALERT + soft dates.
- Novelty gate: day-over-day repeat without worsening → demoted (3-card floor
  backfills). Compares against PREVIOUS report_date only, never same-day.

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
