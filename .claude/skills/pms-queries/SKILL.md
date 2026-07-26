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

## Per-hotel variation

Hotel character lives in `hotels.pms_config` (e.g. `hotel_type: city|resort`
merges lead-time buckets in the compute layer) — NEVER in per-hotel SQL.
One SQL text per PMS, config-driven presentation.
