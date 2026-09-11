---
name: pms-queries
description: Writing or changing PMS SQL queries (Protel/SQL Server today, Opera/Fidelio/Hotelizer/Pylon later) — conventions, cancellation logic, STLY definition, and the checklist for adding a query end-to-end.
---

# PMS queries

All Protel queries: `db/adapters/protel_mssql/queries.py` (Q1–Q13), executed by
`fetcher.py` in the same folder. Queries are versioned per PMS adapter — the
analyst never sees SQL, only the HotelDataSnapshot contract.

## Non-negotiable conventions (every query)

- **Active bookings**: `reschar < 2`. **Cancellations**: `reschar = 2`
  (cancel date in `Canceled`, original book date in `SystemDate`).
- **Fake rooms excluded everywhere**: interpolate `_FAKE_RT_EXCLUDE`
  (categories in `protel.proteluser.kat` where `zimmer = 0`). The fragment
  uses alias `h.` — `.replace("h.", "")` when the query has no alias.
- **STLY** ("same time last year") = stays last year with
  `CAST(SystemDate AS DATE) <= @stly_cap` where `@stly_cap = DATEADD(YEAR,-1,@today)`,
  INCLUDING bookings later cancelled but active at that date:
  `(reschar < 2 OR (reschar = 2 AND CAST(Canceled AS DATE) > @stly_cap))`,
  with Occupancy restored via the `datumbis = date` CASE (cancelled rows have
  Occupancy 0 in Protel).
- **Bounded stateless aggregates only** — no full-history scans, no watermarks,
  no incremental state. Windows: MTD, next 90 days, last 14/28 days, 12 months.
- Bind params are positional `?` (pyodbc); `mpehotel = ?` is the property id —
  count the `?` and pass `hotel_id` that many times in the fetcher.
- Tables available: `bidata.proteluser.Hitia` (bookings),
  `protel.proteluser.kat`, `protel.proteluser.zimmer`. Read-only — the
  firstlight login must never need more than SELECT on schema `proteluser`.

## Checklist: adding a new query end-to-end

1. `queries.py`: add `Q_NAME` with a comment block (what/why/conventions used).
2. `fetcher.py`: execute + shape rows into the snapshot payload.
   **Optional signal data must be fail-open**: wrap in try/except, default to
   empty, print a non-blocking warning (copy the `lead_time` block).
3. `db/contract.py`: core field → `_REQUIRED_CORE`; optional signal field →
   track in `build_data_quality` missing-list + add prefix to
   `_SIGNAL_PREFIXES` (never blocks, never flips legacy_mode) + add to the
   `rows_fetched` tuple.
4. Consume it in `briefing/analyst.py` (see the `analyst-signals` skill).
5. Tests: extend `test_contract.py` expectations if contract changed; compute
   tests in a `test_<feature>.py` script (no pytest).
6. Real-DB validation: cannot run locally — push, then trigger a manual
   refresh and check `refresh_runs.data_quality.rows_fetched.<field>` > 0
   (see `ops-monitoring` skill). Manual refreshes reuse AI, so this is free.
7. `docs/ENGINEERING_LOG.md`: release-history row.

## Opera 5 / Oracle (`db/adapters/opera_oracle/`, since 2026-09-12)

Different shape, same conventions. Source = the hotel group's BI view
`OPERA.EUROTEL_TARGIT_WBLKNM` (one row per reservation per stay night — the
view their Power BI reads, so numbers reconcile by construction). The adapter
runs ONE bounded extract per property (`Q_GRAIN`: stay date × book date ×
cancel date × source × status group, 13 months back → next year end) and
computes every Protel query in Python (`fetcher.compute_pack`). Rules:

- Status is PER NIGHT: `RESERVED`/`CHECKED IN` = occupied night (`NO_ROOMS`,
  `ROOM_REVENUE` net, `+ ROOM_REVENUE_TAX` gross); `CHECKED OUT` = departure-day
  row (0 nights, day-use revenue only); `CANCELLED` = lost night
  (`CANCELLED_ROOM_NIGHTS`, `CLX_ROOM_REVENUE_GROSS`, `CANCELLATION_DATE`).
  `NULL` status / `R_TYPE = 'B'` = unpicked group block → excluded. `NO SHOW`,
  `PROSPECT` excluded.
- **Pseudo rooms (`PSUEDO_ROOM_YN = 'Y'`, PM/PI posting masters): nights
  excluded, REVENUE INCLUDED** — Hotel BI convention, verified day by day.
  NEVER put the pseudo predicate in the WHERE clause: it makes the view scan
  27 s instead of 2 s (predicate pushdown into the UNION). Keep it in the
  SELECT `CASE`.
- Property selector = `RESORT` code (`pms_config.sql.pms_hotel_id`, e.g.
  `"CITY"`). Physical inventory = `RESORT$_ROOM_CATEGORY.NUMBER_ROOMS` where
  `PSUEDO_ROOM_TYPE IS NULL`.
- Night-audit gate: `OPERA.BUSINESSDATE.STATE` for yesterday must be
  `CLOSED`, else fetch raises (retry ladder handles it).
- Binds are named (`:resort`), driver = python-oracledb thin (no client libs).
- Validate with `scripts/validate_opera.py` (VPN or local cloudflared port):
  native `RESERVATION_STAT_DAILY` cross-check + Hotel BI candidates.
  Unit tests: `py -3.13 test_opera.py` (synthetic grain rows).

Shared payload assembly for ALL adapters: `db/adapters/assemble.py` — an
adapter only has to produce the Protel-shaped result rows.

## Per-hotel variation

Hotel character lives in `hotels.pms_config` (e.g. `hotel_type: city|resort`
merges lead-time buckets in the compute layer) — NEVER in per-hotel SQL.
One SQL text per PMS, config-driven presentation.
