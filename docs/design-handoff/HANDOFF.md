# FirstLight — Front-end Design Handoff

**Audience:** the designer/AI rebuilding this as the FirstLight mobile app front end
(target: React, deployed on Cloudflare Pages — "Phase B" of the roadmap).
**What FirstLight is:** a morning briefing for hotel GMs/revenue managers — yesterday's
KPIs, pickup, pace, an AI hero paragraph + insight cards — generated server-side each
morning, read on a phone, standing up, in under a minute.

## Files in this bundle

| File | What |
|---|---|
| `sample-briefing-live.html` | The REAL production briefing as rendered today (real hotel data). This is the ground truth for current look & content — open it on a 390–430px viewport |
| `template-email.html` | The Jinja2 source that generates it (today's "front end" — server-rendered). Contains all CSS tokens and section markup. NOTE: this same template minus the `fl-*` chart cards is also sent as the daily EMAIL — the email stays server-rendered; do NOT port the email |
| `charts-logic.py` | The compute layer for the 5 newest chart cards — every displayed value/width is precomputed here; in React these become the props contract |

## Design tokens (the app's design language — keep it)

- **Fonts:** Outfit (300–800) for UI, IBM Plex Mono (400–600) for all numerals/labels.
  All numbers use `tabular-nums`.
- **Core palette:** navy `#0F2860` / deep `#0A1F4D` (primary data), blue `#2E7CF7`
  (accent/secondary data), teal `#38E1F0` (brand gradient end, used sparingly),
  green `#1A7A50` (ahead/won), red `#B83A1B` (behind/lost), amber `#B47D09`
  (needed/attention), greys `#F7F8FB → #4D5A74`, text `#1A2540`.
- **Gradients:** hero `135deg #0A1F4D → #2E7CF7`; cyan accents `#2E7CF7 → #38E1F0`.
- **Surfaces:** white cards, 1px `#E2E7F0` hairline borders, radius 10–16px,
  background `#F0F2F7`. Elevation is rare — shadows only on hero-level elements.
- **Semantic color rules (strict):** blue = neutral data · red ONLY when behind LY ·
  green ONLY for ahead/won states · amber = required/attention. In the ADR bridge,
  mix (blue) and rate (amber) must NEVER share a visual language.
- Max content width ~640px; charts designed for 360–430px. Row tap targets ≥44px.

## Page structure (top → bottom, as in the sample)

1. **Tab nav** (sticky): Overview · Pickup · Pace · AI Insights — scroll anchors
2. **Header**: brand + hotel + date/generated-at
3. **Smart Summary (HERO)**: dark gradient card, the AI morning paragraph
   (4–6 sentences, starts "Good morning.") — the emotional center of the product
4. **Yesterday KPI row** (4 cards): revenue, occupancy, ADR, room nights — each vs LY
5. **MTD strip** (4 cells)
6. **On The Books — next 3 months** (cards: rev, rn, occ, vs STLY)
7. **Pickup Activity**: Today / Yesterday / 3-Day / 7-Day booked-cancel-net cards
8. **Booked vs cancelled (butterfly)** — replaces the old "Top month" widget
9. **Booking speed (velocity)** — rooms/day
10. **Pace charts** (existing SVG): Revenue OTB bars, Occupancy lines (closed months
    show STLY only — Final-LY dash starts at current month), ADR bars
11. **ADR bridge** — mix vs rate decomposition
12. **Where each month stands (curve position meter)**
13. **Demand heat — next 60 days** (continuous calendar)
14. **Top channels** (bar list) · **Next 7 days** table
15. **AI insight cards** (3–5): tag (ALERT/OPPORTUNITY/MONITOR), headline,
    2 evidence KPIs, what happened, why it matters, recommended action, BY WHEN,
    AT-STAKE (value + tappable calculation). Word caps are a hard backend contract:
    headline 12 / what 20 / why 35 / action 25 / by-when 10 words — design for
    those maxima.
16. Footer

## The 5 chart cards — decided specs (verbatim from the product owner)

All five live in `sample-briefing-live.html` exactly as approved. Key rules:

1. **Curve position meter** (§12): per future month — grey track = LY final
   (end labeled), navy bar = booked now, grey tick = "LY same date" (labeled
   inline, no legend lookup), right column "+X rn vs LY pace" + "Y rn to reach
   LY final". Bar RED only when behind LY pace. When booked > LY final the bar
   BREAKS THROUGH the track end and spills right in GREEN, labeled "+Z rn above
   LY final". All units room nights — never percentage points.
2. **Velocity** (§9): TWO bars per month (last 7d navy, last 14d blue), current
   + next 3 stay months, values "X.X /day · 7d|14d", grey tick = LY speed,
   amber tick + "need X/day" → replaced by green "✓ passed LY final (+rn)" once
   the month beats LY. Third line: speeding up / slowing down / steady.
3. **Butterfly** (§8): cancellations fly LEFT (red 7d, lighter red 14d),
   bookings RIGHT (navy 7d, blue 14d), thin paired arms per month, shared
   scale, center axis, 4-swatch legend on top, net per window on the right
   (red when cancels > 60% of gross), amber alert names the worst-churn month.
4. **ADR bridge** (§11): four floating bars — LY ADR (grey), MIX step (blue),
   RATE step (amber), TY ADR (navy) — plus a generated narrative sentence
   (never free-form; from the structured payload) and a 5-row channel drill
   table (share LY→TY, ADR LY→TY, mix, rate). Suppressed entirely if the
   identity check fails.
5. **Demand heat** (§13): next 60 days as ONE CONTINUOUS 7-column weekday
   calendar (Mon–Sun header), each cell shows occupancy % (top) + date dd/mm
   (below); shade = occupancy (7-step single-blue ramp — colour-blind safe);
   red inset outline = date far behind LY (also named in an amber alert line);
   month change = SMALL NAVY BORDER on the 1st's cell (no divider rows).

## Data contract (what the React app will consume)

Today the PWA displays `rendered_html`. The rebuild renders from JSON instead:

- `briefings.data` — the snapshot: `yesterday{}`, `mtd{}`, `pickup{}`, `pace[]`,
  `pickup_daily[]`, `otb_by_date[]`, `lead_time[]`, `cancel_daily[]`,
  `consumed_by_source[]`, `topChannels[]`, `next7days[]`, `total_rooms`
- `briefings.ai_insights` — `executive_summary` (the hero paragraph) +
  `insights[]` (both new card anatomy and legacy fields)
- `charts-logic.py` shows exactly how raw data becomes each chart's display
  values — port this logic, don't reinvent it
- API will be FastAPI with per-hotel tokens (Phase A); until then Supabase REST

## New UI features to design INTO the rebuild (roadmap, already decided)

- ⓘ info button on EVERY section → tooltip/sheet explaining how to read it
  (copy provided; bilingual from day one)
- Greek / English toggle (all fixed UI text via translation file; Greek runs
  ~15–25% longer — test fit)
- Text-size setting: whole-report scale, 5 steps (like phone accessibility)
- 👍/👎 on every AI insight card + optional reason chips ("knew it already",
  "not actionable", "numbers look wrong", "too late")
- 7-day history: swipe/dropdown to previous briefings + ▲▼ deltas vs yesterday
- Multiproperty switcher must reset scroll to top on hotel change
- Section share (rasterise to PNG → native share sheet) — exists today, keep
- Tap-throughs (design the destinations): meter row → month detail; velocity
  row → month detail; butterfly row → cancellation list; heat cell → stay-date
  detail; AT-STAKE chip → calculation breakdown

## Hard constraints

- Mobile-first; must read standing up in under a minute; summary before detail
- Never encode meaning in colour alone (pair with sign/glyph/text)
- No dual axes; no chart libraries needed — everything is divs + small inline SVG
- The EMAIL remains server-rendered from the Jinja template — out of scope here
- PWA essentials: installable, push notifications, offline-cache last briefing
