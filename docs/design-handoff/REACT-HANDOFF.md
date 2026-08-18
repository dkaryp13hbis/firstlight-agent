# FirstLight — React App Design File (v2.1 · 2026-08-15)

The canonical design reference for the React app (`firstlight-pwa/web/`),
after the legacy-parity pass. Legacy briefing V2.0 is the design target;
this file records the implemented state. Source of truth for values:
`web/src/index.css` (tokens) + the component files listed at the end.

## 1. Global tokens

```css
--page:       #EAEDF1;          /* page background */
--card-bg:    #FFFFFF;
--card-shadow: 0 1px 3px rgba(10,20,45,.08), 0 8px 20px rgba(10,20,45,.05);
--r-card:     18px;             /* card radius */
--sec-gap:    22px;             /* gap between sections */
/* card inner padding: 18–20px */

--app-top:    #061535;          /* header/footer navy */
--navy:       #0F2860;  --navy-deep: #0A1F4D;
--blue:       #2E7CF7;  --cyan: #38E1F0;
--grad-cyan:  linear-gradient(135deg,#2E7CF7 0%,#38E1F0 100%);
--green:      #1A7A50;  --green-bg: rgba(26,122,80,.05);
--red:        #B83A1B;  --red-bg:   rgba(184,58,27,.05);
--text:       #1A2540;          /* ink */
--n700: #15296B; --n600: #4D5A74; --n500: #6E7A96; --cap: #79747E;
--grey-50: #F7F8FB; --grey-100: #F1F3F8; --grey-200: #E2E7F0;
AMBER (charts): #B47D09 · heat anomaly ring: #BA1A1A
```

## 2. Typography — ONE system

- **Manrope only** (self-hosted woff2, all subsets incl. Greek; Outfit 700
  only for the logo wordmark). No mono fonts anywhere.
- Weight rules (GLOBAL TYPE RULES): **800 = data values · 700 = deltas,
  chips, badges, month labels · 600 = captions/labels**.
- KPI values 26px/-.03em · OTB month value 26px · card titles 14px/700 ·
  section labels 13.5px/700 `#0F2860` · captions 10–10.5px/600.

## 3. Chrome

- **Sticky** navy header + tab bar (one sticky wrapper, z 999); navy runs
  under the status bar (`theme-color #061535`, `black-translucent`).
- Header row 1: canonical **lockup B** (verbatim SVG, never redraw) left;
  right = 3 round 36px icon buttons: 🔔 (cyan ring), share (node icon), ⚙.
- Row 2: `Hotel` label + picker pill (dark dropdown `#0A1F4D`) + `↻ Refresh`.
- Tabs: `Overview · Pickup · Pace · AI Insights (n)`, spread evenly on white;
  active = `#1E5FD0` label + 2px `#2E7CF7` underline.
- **Section header** (every section): leading 15px blue SVG icon + title +
  ⓘ (18px circle, border `#A0AAC0`) + right-aligned `↑ Share` outline pill.
- Footer: gradient CTA `Open Hotel BI for full report →`, feature sub-line,
  `FIRSTLIGHT · AI MORNING BRIEFING V2.0` meta, source/generated line,
  closing navy band.
- **Settings sheet** rows: Language `EN | ΕΛ` · Revenue `Gross | Net`
  (default Gross) · Reporting year `2026 | 2027` · Comparison year (2025 fixed
  when 2026; `2026 | 2025` when 2027) + one-line semantics caption · Text size
  `A− n/5 A+` · red `Sign out`. All segmented controls = same navy pill. Net mode swaps every revenue + ADR figure
  (payload `revenueNet` / `rev_net`, ADR = net ÷ nights) and shows a slim
  `NET` pill strip under the hero; hero/AI narrative text stays gross.

## 4. Modules (implemented spec)

- **Smart Summary**: navy card, `radial-gradient(90% 100% at 92% 0%,
  rgba(56,225,240,.4), rgba(46,124,247,.16) 40%, transparent 70%) +
  linear-gradient(160deg,#0F2860,#0A1F4D)`. Label row: SMART SUMMARY tag +
  ⓘ + `Last refresh 15 AUG · 22:09` (short, upper) + 🔊. Collapsed:
  21px/800 verdict headline + Performance / Pickup / On the Books rows
  (9.5px cyan uppercase labels, values `<b>` white 800) + `Read the full
  briefing →`; expanded = full hero text + `Show less ↑`.
- **Yesterday**: section header + **2×2 grid**; card = 3px `--grad-cyan`
  top border, uppercase letterspaced 10px label `#15296B`, 26px value,
  ▲/▼ delta, `vs LY …` caption.
- **MTD**: 4 discrete white cards in a row.
- **OTB next 3 months**: full-width **stacked** cards; 4px status top border
  (grad-cyan ahead / red behind), uppercase month, 26px value,
  `N rn · x% occ`, `▲ +x% vs STLY`.
- **Pickup**: 2×2 tappable boxes (canon anatomy: colored uppercase title,
  Booked/Cancel/Net rows); selection = 1.5px animated gradient ring
  (`120deg #0F2860→#2E7CF7→#38E1F0→…`, 3.5s). Butterfly: dynamic window
  title with date range, `◀ CANCELLED / BOOKED ▶` axis row, single pair per
  month (booked #0F2860 right, cancelled #B83A1B left), amber churn callout
  (`#FBEEDC`/`#6D4C00`, ≥15% rule), cancelled-revenue strip.
- **Pace charts** (`Revenue OTB` / `Occupancy` / `ADR`): right-aligned
  square-swatch legend `OTB TY / STLY / Final LY`; y-axis ticks (13px) +
  gridlines `#EBEEF4`; bars navy (green when ≥ final LY), STLY `#CDD4E0`,
  rx 1.5; green dashed final-LY ticks on open months only; month labels
  15px; **variance pills** 54×22 tinted, 14.5px/800. Occupancy: navy line
  3px + `rgba(15,40,96,.07)` area fill + white-chip labels 13.5px/800
  (green when ≥ final LY); dashed final line from current month.
- **Where each month stands**: explainer paragraph; per month: grey track =
  LY final, navy bar (red when behind LY pace), green spill past track end;
  STLY tick; labeled trio `booked / ▲ LY same date / LY final`; right stat
  block `+N rn vs LY pace` + above/below-final line.
- **Booking speed**: legend `last 7 days / last 14 days / needed for LY
  final (amber dash)`; month rows with trend word (red `slowing down` /
  grey `steady` / green `speeding up`); bars 7d navy, 14d `#7FB0FA`; amber
  needed marker; `✓ passed LY final (+N rn)` green; values `x.x /day·7d`.
- **Demand heat — next 60 days**: subtitle line; weekday initials
  `M T W T F S S`; cells = 8b buckets over ramp `#F2F2F7→#0A6CDF`, white fg
  from bucket 4; `NN%` 13.5px/800 + `dd/mm` 9.5px; month change = inset
  2px left border `#1D1B20`; anomaly = inset 2px ring `#BA1A1A`; ramp
  legend + `⚠ Dates far behind last year: …` note.
- **ADR bridge — why the rate moved**: mix/rate explainer; headline ADR vs
  LY + delta; table `CHANNEL | SHARE (44% → 54%) | ADR (€564 → €517) |
  MIX | RATE` (MIX header `#2E7CF7`, RATE `#B47D09`); identity-guarded.
- **Top Sources**: 92px wrapping name column, 6px grad-cyan bar +
  STLY tick, rev + STLY rev, tinted ▲/▼ badge.
- **AI Insights**: card = white, 1px `rgba(10,31,77,.08)` border, r16,
  padding `20/20/16/24`, left 3px gradient stripe (ALERT `#C7411B→#E0A82E`,
  OPPORTUNITY `#2E7CF7→#38E1F0`, MONITOR `#7C5BFF→#38E1F0`); title
  14px/700 `#0A1F4D`; **uppercase letterspaced pill** with tinted border;
  chevron SVG; expanded = evidence sub-cards, What happened / Why it
  matters / Suggested review (+ by when, at stake), `Was this useful? 👍👎`
  → note sheet.
- **Sheets** (settings/feedback): white bottom sheet r22 top, drag handle,
  title 20px/800 `#0F2860`, rows split by `#EDF0F6`; EN/ΕΛ segmented
  (active navy), A− x/5 A+, red Sign out.

## 5. Hard rules

1. **Never redraw the logo** — lockup B geometry is canonical, copy verbatim.
2. Any styling question → read the canon (`template-email.html` here +
   this file), never from memory. Verify the SHIPPED bundle, not the source.
3. One type system; add elements to weight groups, never one-off weights.
4. Charts: no chart libraries — hand-built SVG per this spec.
5. Every visual computed client-side from briefing JSON (render-from-data).

## 6. Source map (`firstlight-pwa/web/src/`)

| File | Contains |
|---|---|
| `index.css` | tokens, type groups, card base |
| `fonts.css` + `public/fonts/` | self-hosted Manrope/Outfit |
| `components/Shell.tsx` | chrome: logo, icons, picker, tabs |
| `components/SmartSummary.tsx` | hero card + sections + voice |
| `components/Overview.tsx` | SectionLabel(+icons/share), Yesterday 2×2, MTD, OTB stacked |
| `components/Pickup.tsx` | pickup boxes + butterfly + callout |
| `components/Charts.tsx` | pace charts, month stands, speed, heat, bridge, sources |
| `components/AiCards.tsx` | insight cards + thumbs |
| `components/Sheets.tsx` / `Info.tsx` / `Login.tsx` | sheets, ⓘ texts, login |
| `api.ts` / `lib/sb.ts` / `types.ts` | data layer (API → Supabase → fixture) |
