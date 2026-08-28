# My Watchlist — build plan (drafted 2026-08-24)

> Let the GM tell FirstLight what to follow. Every refresh, each watched item
> gets a deterministic one-line update, whether or not the ranker thinks it
> matters today. Zero Claude. Slots only — no arithmetic in text.

Status: **v1 BUILT 2026-08-24** (PWA repo, uncommitted at time of writing;
SQL `docs/sql/2026-08-24_watchlist.sql` awaiting paste). ⚑ decisions were
taken with the recommended defaults: per user · after the MTD strip ·
demo-gated first. Deviations from this spec in the build: no Note editing
yet (column exists); "+ Watch" is a dashed row under the cards rather than
in the section header; the tap on a card goes to the Pace tab.

---

## 1. Why

- Card ranking (R×U×M×N×C) is automatic; the PMS can't know about the board
  meeting on October or the wedding block Sep 22–28. The thing the GM cares
  most about may be card #6 — below the 3-card cut.
- 👍/👎 tunes ranking slowly and only for cards that already surfaced.
  Watchlist is the direct version: "show me October every morning."
- It is the personal half of "FirstLight remembers what mattered"; the story
  lifecycle (status enum, separate item) is the automatic half.

## 2. Scope v1 (this build)

| In | Out (later) |
|---|---|
| Watch a **stay month** (`2026-10`) | Watch a **source/channel** (v1.1 — `topChannels` has no daily history yet) |
| Watch a **date range** (`2026-09-22..2026-09-28`, inside the 90-day `otb_by_date` window) | Watch a **Signal/card** (v1.1 — becomes trivial once the story `status` block exists) |
| "Your watchlist" section on Today, one line per item, updates every refresh | Watchlist lines in the **morning push** (v2 — push body is per hotel today, would need per-user bodies) |
| Add from: section "+ Watch" sheet, OTB month card, AI card footer, **heatmap cell** (tap = date, second tap = range; offers date / this week / flagged soft run — built 2026-08-24) | Add from pace-bar tooltip / Booking Speed rows (later) |
| Per-**user** storage in Supabase, localStorage for the demo hotel | Backend awareness (watched month never demoted by the novelty gate — v2) |
| EN + EL strings via the same dictionary the UI uses | 30-day slim history via widened `kpi_summary` (§7) — the 7-day trend strip from stored rows was built 2026-08-25 |

Cap: **5 items per user per hotel** (keeps the section a glance, not a list).

## 3. Data — everything needed is already in the payload + one stored row

| Need | Source (no new PMS query) |
|---|---|
| Month: rn / rn_stly / rn_final_ly / rev / rev_stly / occ | `data.pace[]` (month_num) |
| Month: net rooms booked yesterday, last 7 days | `data.pickup_daily[]` (ref_date × stay_month × net_rn) — already used by Smart Summary + BookingSpeed |
| Month: cancellations last 7 days | `data.cancel_daily[]` |
| Month: booking speed 7d/14d, needed/day to reach LY final, days left | same math as `BookingSpeed` in `Charts.tsx` (mirror of `charts._velocity`) — extract into a shared helper |
| Range: rn_ty / rn_stly per stay date, occupancy = rn / total_rooms | `data.otb_by_date[]` (90 days), `data.total_rooms` |
| **"since yesterday"** for month gap % and for ranges | the **previous report_date row** — `fetchDates(hotel, 2)[1]` + `fetchBriefingByDate` (both exist for the 7-day history). One extra fetch per load, cached in localStorage next to `fl_briefing_*`. Fallback when the row is missing: month delta from `pickup_daily` (ref_date = yesterday), range delta omitted. |

"Yesterday" = previous **report_date** (a manual refresh overwrites the day's
row — same convention as the novelty gate and the history strip).

## 4. Storage

`docs/sql/2026-08-2x_watchlist.sql` (user pastes; app tolerates absence —
insert failure → toast "Watchlist not available yet", section hidden):

```sql
create table if not exists watchlist (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid(),
  hotel_id    uuid not null,
  kind        text not null check (kind in ('month','range')),
  key         text not null,          -- 'YYYY-MM' | 'YYYY-MM-DD..YYYY-MM-DD'
  label       text,                   -- optional user label ("Board meeting", "Wedding")
  note        text,
  created_at  timestamptz not null default now(),
  unique (user_id, hotel_id, kind, key)
);
alter table watchlist enable row level security;
-- own rows only (select/insert/delete); service role reads all (v2 push)
create policy wl_select on watchlist for select to authenticated using (user_id = auth.uid());
create policy wl_insert on watchlist for insert to authenticated with check (user_id = auth.uid());
create policy wl_delete on watchlist for delete to authenticated using (user_id = auth.uid());
```

⚑ **Per user, not per hotel** (recommended): GM and revenue manager watch
different things; multi-hotel owners watch per property. `hotel_prefs`
(per hotel) is the wrong home for this.

Demo hotel (`hotelId === 'demo'`): localStorage `fl_watch_demo`, same shape.

## 5. UI

### 5.1 The section — "Your watchlist"

Placement ⚑ (recommended): **directly after `MtdStrip`, before `OtbCards`** —
i.e. after the "today" facts, as the first forward-looking block. (The doc
proposes "under Signals"; Signals are at the bottom today, and moving them is a
separate item. If Signals move under the hero later, the watchlist follows.)

Rendered only when the user has ≥1 item; otherwise a single quiet row
"Watch a month or a date range → " (opens the add sheet) — shown once per
session, dismissable.

Standard `SectionLabel` (icon 👁 / title "Your watchlist" / ⓘ / Share), then one
white card per item:

```
OCTOBER                                    IMPROVING ▲
620 rooms booked · 21% behind same time last year (was 24% yesterday)
+9 rooms since yesterday · +31 last 7 days · 12 cancelled
Speed 4.0/day · need 14.3/day to reach last year's final (68 days left)
                                                       Remove · Note
```

```
SEP 22 – 28  "Wedding"                                  STEADY —
61% booked vs 78% same time last year · 302 rooms
+2 rooms since yesterday · lowest date Thu 25 (48%)
```

Tap a card → scrolls to that month in the OTB charts / to the heatmap. The
status pill uses the Smart Summary `StatePill` tones (mint / coral / neutral).

Hidden in the past-day view (`viewDate != null`) — v1 shows watches as-of today only.

### 5.2 Status word (deterministic, per item)

| Item | Metric compared to yesterday | IMPROVING | GETTING WORSE | else |
|---|---|---|---|---|
| month | gap % vs STLY (`(rn − rn_stly) / rn_stly`) | Δ ≥ +1.0 pt | Δ ≤ −1.0 pt | STEADY |
| month, once `rn ≥ rn_final_ly` | — | "✓ PASSED LAST YEAR'S FINAL" | | |
| range (v1.2, 2026-08-28) | today's net rooms vs last year's net on the same day at the same lead time (`Δrn_ty − Δrn_stly` between yesterday's and today's briefing) | ≥ +tol | ≤ −tol | STEADY — tol = max(2, 0.5% of the range's room-nights) |
| any, no previous row | — | pill omitted, line says "first day watching" | | |

Same 15%-style hysteresis philosophy as elsewhere: small moves read as steady.

### 5.3 Copy — slots only (EN / EL keys in the UI dictionary)

Month line 1: `{rn} rooms booked · {gap}% {behind|ahead of} same time last year{ (was {gap_prev}% yesterday)}`
Month line 2: `{net_1d:+} rooms since yesterday · {net_7d:+} last 7 days · {cancel_7d} cancelled`
Month line 3: `Speed {v7}/day · need {need}/day to reach last year's final ({days_left} days left)` — or `✓ passed last year's final (+{over} rooms)`
Range line 1: `{occ}% booked vs {occ_ly}% same time last year · {rn} rooms`
Range line 2: `{net_1d:+} rooms since yesterday (last year: {net_1d_ly:+} on the same day) · lowest date {weekday dd} ({occ_min}%)`

Plain-language rule applies: "rooms booked", "same time last year", never
OTB/STLY/pace in prose. No € figures beyond real PMS revenue (none needed here).

### 5.4 Entry points (v1)

1. **"+ Watch" in the section header** → `WatchSheet`: month chips (current + next 11 open months from `pace`, already-watched greyed) or a date-range picker limited to the next 90 days (two date inputs; presets "this weekend", "next 7 days"); optional label. Save → row insert → section updates immediately.
2. **OTB month card** (`OtbCards`) → small 👁 icon on each month card; tap = watch/unwatch that month (toast "Watching October").
3. **AI card footer** (`AiCards`, next to 👍/👎) → "Watch" when the card is month-scoped (card id pattern `*_oct_2026`, `*_sep_2026`; the month is parsed from the id). Adds a month watch.

Remove: swipe/“Remove” on the card, or unwatch from the same 👁 toggle.

### 5.5 Lifecycle

- Month watch: when the month closes (report month > watched month) the card
  shows once as `CLOSED — finished {rn} rooms, {gap}% vs last year` with
  "Remove"; auto-removed after that day.
- Range watch: same, once `end_date ≤ report_date` (report_date = yesterday and `otb_by_date` only carries stay dates from today, so `end_date == report_date` has no rows either — was showing the "beyond the 90-day window" text for one day).
- NOTE (2026-08-28): the "auto-removed after that day" step is NOT implemented in the app yet — closed cards stay until the user taps Remove. TO-DO.
- Hotel switch: list is per hotel; loads with the briefing.

### 5.6 Tracking (`lib/track.ts`)

`watch_add {kind, key, from: sheet|otb_card|ai_card}`, `watch_remove {kind}`,
`watch_tap {kind}`. This is also the best future signal of what a GM cares
about — cleaner than 👍/👎.

## 6. Code map (PWA repo `../firstlight-pwa/web/src`)

| File | Change |
|---|---|
| `lib/watch.ts` (new) | types, `computeWatchLines(briefing, prevBriefing, items)` — the rule ladder + slot filling, pure function, unit-testable with `fixtures/briefing.json` |
| `lib/speed.ts` (new) | extract the 7d/14d/needed-per-day math from `Charts.tsx:BookingSpeed` so the chart and the watchlist can never disagree |
| `api.ts` | `fetchWatchlist(hotelId)`, `addWatch`, `removeWatch` (Supabase; localStorage for demo); `fetchPrevBriefing(hotelId)` = `fetchDates(…,2)` + `fetchBriefingByDate`, cached `fl_prev_{hotel}` |
| `components/Watchlist.tsx` (new) | section + item cards + `WatchSheet` (uses `Sheet` from `Sheets.tsx`) |
| `components/Overview.tsx` | 👁 toggle on `OtbCards` month cards |
| `components/AiCards.tsx` | "Watch" in the footer for month-scoped cards |
| `App.tsx` | load watchlist + prev row with the briefing; mount `<Watchlist>` after `<MtdStrip>`; hide in past view; reset on hotel switch |
| `types.ts` | add `pickup_daily`, `cancel_daily`, `otb_by_date` to `BriefingData` (they're read via casts today in three places — worth typing once) |
| i18n dictionary | ~20 keys (EN now, EL when the dictionary lands) |

Backend (this repo): **nothing in v1.** SQL file only.

## 7. Follow-ups (not v1)

- **v1.1 — trend line per item** (7-day gap sparkline): widen
  `cloud_push._kpi_summary` with `pace_rn: {month_num: {rn, rn_stly}}` and
  `otb_dates: [{d, rn, rn_stly}]` (60 × 3 ints) so `/briefing/history` carries
  it — still slim, no HTML. Then the watchlist reads 7 slim rows instead of 1 fat one.
- **v1.1 — heatmap cell → "Watch this week"**; **source watch** once
  `topChannels` history exists.
- **v2 — morning push line** for watched items that moved ≥ 5% (backend reads
  `watchlist` with the service role; per-user push bodies; reuse the
  claim-then-send cap from `briefing/intraday.py`).
- **v2 — novelty gate**: a watched month's card is never demoted for that
  user (needs per-user card views — only meaningful once the API is the sole
  read path).
- **Decision Journal** attaches to the same rows (Reviewing / Assign / Note);
  the `note` column is the seed.

## 8. Effort

Front-end ~1.5 days (compute lib ½, section + sheet ½, entry points + wiring
½), SQL 10 min, test against `fixtures/briefing.json` plus a real Pome row.
Ship behind the demo-account gate first (same pattern as usage tracking),
then open to both hotels.
