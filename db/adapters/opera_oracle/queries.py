"""
Opera 5 (on-prem, Oracle) — SQL for the FirstLight adapter.

Source of truth: the hotel group's own BI view `OPERA.EUROTEL_TARGIT_WBLKNM`
(one row per reservation per stay night, the same view their Power BI model
reads — so our numbers reconcile with theirs by construction). Validated
2026-09-11 against Opera's native `RESERVATION_STAT_DAILY`: room nights
identical, room revenue within 1% (Aug 2026, all three properties).

Row semantics of the view (probed 2026-09-11, Opera 5 / Oracle 19c):
  BUSINESS_DATE           stay night (the "date" axis)
  BOOKED_DATE             booking timestamp (Protel SystemDate)
  CANCELLATION_DATE       cancellation timestamp (Protel Canceled)
  RESV_STATUS  per NIGHT: RESERVED / CHECKED IN = an occupied night (NO_ROOMS
               = rooms, ROOM_REVENUE = net, + ROOM_REVENUE_TAX = gross);
               CHECKED OUT = the departure-day row (0 nights; carries day-use
               revenue only); CANCELLED = lost night (CANCELLED_ROOM_NIGHTS,
               CLX_ROOM_REVENUE_GROSS); NO SHOW / PROSPECT = ignored;
               NULL status + R_TYPE 'B' = unpicked group block → excluded.
  PSUEDO_ROOM_YN = 'Y'    pseudo rooms (PM / PI / catering posting masters)
                          — Opera's equivalent of Protel's kat.zimmer = 0.
                          NIGHTS exclude them; REVENUE includes them: room
                          revenue posted to a paymaster is real revenue with
                          no physical night (Hotel BI convention — verified
                          day by day, City Hotel Jan 2026: 275,469 € exact,
                          pseudo-excluded was 3,585 € short; nights identical).
  SOURCE_CODE             booking source (Protel Sourcen); NULL → 'Direct'.
  RESORT                  the property (Protel mpehotel) — multiproperty DB.

Cancellation logic mirrors Protel exactly (see protel_mssql/queries.py):
  active            = status in (RESERVED, CHECKED IN, CHECKED OUT)
  cancellation      = status CANCELLED (cancel date on the row, original
                      book date kept) — used on the book-date axis only
  STLY              = stays last year booked by the same date last year,
                      INCLUDING bookings later cancelled but active at that
                      date (cancel_date > stly_cap), nights restored from
                      CANCELLED_ROOM_NIGHTS, revenue from CLX_ROOM_REVENUE_GROSS.

Performance: the view is a heavy UNION (a single book-date scan took 15 s),
so instead of 16 separate queries the adapter pulls ONE bounded aggregate
per property (Q_GRAIN, ~35–90k rows, 2–10 s) and computes the whole pack in
Python (`fetcher.py`). Bounded window: 1 Jan of last year −1 month → 31 Dec
of next year. Stateless, no watermarks.

Binds are named (`:resort`) — python-oracledb thin driver.
"""

VIEW = "OPERA.EUROTEL_TARGIT_WBLKNM"
PHYS = "NVL(psuedo_room_yn, 'N') = 'N'"   # physical room (not a posting master)

# ------------------------------------------------------------------
# Q0: Night-audit gate — the night we report on must be CLOSED
# ------------------------------------------------------------------

Q_BUSINESS_DATE = """
SELECT business_date, state
  FROM OPERA.BUSINESSDATE
 WHERE resort = :resort
   AND business_date >= :day
 ORDER BY business_date
"""

# ------------------------------------------------------------------
# Q7: Physical inventory — non-pseudo room categories (the group's own
# room-type model: RESORT$_ROOM_CATEGORY.NUMBER_ROOMS, PSUEDO_ROOM_TYPE NULL)
# ------------------------------------------------------------------

Q_INVENTORY = """
SELECT NVL(SUM(number_rooms), 0) AS total_rooms
  FROM OPERA.RESORT$_ROOM_CATEGORY
 WHERE resort = :resort
   AND psuedo_room_type IS NULL
"""

# ------------------------------------------------------------------
# Q_MIX_GRAIN (2026-10-30): the booking-MIX extract — stay year/month × book
# date × cancel date × source × market segment × rate code × room type ×
# status group. Columns = the group's own Power BI query on this view
# (MARKET_CODE "Market", RATE_CODE "Rate Code", ROOM_TYPE "Room Type
# Short"); codes kept as written, blanks → '(none)'. Same row rules as
# Q_GRAIN (physical-room nights, gross revenue incl. posting masters, the
# pseudo predicate in the SELECT CASE, never in WHERE). A separate, optional
# query so the main grain stays as fast as it is; stay MONTH (not date)
# keeps it small. fetcher.compute_mix applies the STLY rules in Python.
# ------------------------------------------------------------------

Q_MIX_GRAIN = f"""
SELECT EXTRACT(YEAR FROM business_date)                          AS stay_year,
       EXTRACT(MONTH FROM business_date)                         AS stay_month,
       TRUNC(booked_date)                                        AS book_date,
       TRUNC(cancellation_date)                                  AS cancel_date,
       NVL(source_code, 'Direct')                                AS source,
       NVL(TRIM(market_code), '(none)')                          AS segment,
       NVL(TRIM(rate_code), '(none)')                            AS rate_plan,
       NVL(TRIM(room_type), '(none)')                            AS room_type,
       CASE WHEN resv_status IN ('RESERVED', 'CHECKED IN') THEN 'A'
            WHEN resv_status = 'CHECKED OUT'                THEN 'D'
            ELSE 'C' END                                         AS sg,
       SUM(CASE WHEN {PHYS} THEN NVL(no_rooms, 0) ELSE 0 END)    AS rn,
       SUM(NVL(room_revenue, 0) + NVL(room_revenue_tax, 0))      AS rev_gross,
       SUM(CASE WHEN {PHYS} THEN NVL(cancelled_room_nights, 0) ELSE 0 END) AS cxl_rn,
       SUM(NVL(clx_room_revenue_gross, 0))                       AS cxl_gross
  FROM {VIEW}
 WHERE resort = :resort
   AND r_type = 'R'
   AND resv_status IN ('RESERVED', 'CHECKED IN', 'CHECKED OUT', 'CANCELLED')
   AND business_date >= ADD_MONTHS(TRUNC(SYSDATE, 'YYYY'), -12)
   AND business_date <  ADD_MONTHS(TRUNC(SYSDATE, 'YYYY'), 24)
 GROUP BY EXTRACT(YEAR FROM business_date), EXTRACT(MONTH FROM business_date),
          TRUNC(booked_date), TRUNC(cancellation_date),
          NVL(source_code, 'Direct'), NVL(TRIM(market_code), '(none)'),
          NVL(TRIM(rate_code), '(none)'), NVL(TRIM(room_type), '(none)'),
          CASE WHEN resv_status IN ('RESERVED', 'CHECKED IN') THEN 'A'
               WHEN resv_status = 'CHECKED OUT'                THEN 'D'
               ELSE 'C' END
"""

# ------------------------------------------------------------------
# Q_GRAIN: the single bounded extract every Protel query is computed from.
# Grain = stay night × book date × cancel date × source × status group ×
# has-revenue. Sums: nights (physical rooms only), net/gross revenue (ALL
# rooms incl. posting masters), cancelled nights (physical) + lost gross
# (all), arrival/departure rooms (physical).
# ------------------------------------------------------------------

Q_GRAIN = f"""
SELECT TRUNC(business_date)                                     AS stay_date,
       TRUNC(booked_date)                                       AS book_date,
       TRUNC(cancellation_date)                                 AS cancel_date,
       NVL(source_code, 'Direct')                               AS source,
       CASE WHEN resv_status IN ('RESERVED', 'CHECKED IN') THEN 'A'
            WHEN resv_status = 'CHECKED OUT'                THEN 'D'
            ELSE 'C' END                                        AS sg,
       CASE WHEN NVL(room_revenue, 0) > 0 THEN 1 ELSE 0 END     AS has_rev,
       SUM(CASE WHEN {PHYS} THEN NVL(no_rooms, 0) ELSE 0 END)   AS rn,
       SUM(NVL(room_revenue, 0))                                AS rev_net,
       SUM(NVL(room_revenue, 0) + NVL(room_revenue_tax, 0))     AS rev_gross,
       SUM(CASE WHEN {PHYS} THEN NVL(cancelled_room_nights, 0) ELSE 0 END) AS cxl_rn,
       SUM(NVL(clx_room_revenue_gross, 0))                      AS cxl_gross,
       SUM(CASE WHEN {PHYS} THEN NVL(arrival_rooms, 0) ELSE 0 END)   AS arr_rooms,
       SUM(CASE WHEN {PHYS} THEN NVL(departure_rooms, 0) ELSE 0 END) AS dep_rooms
  FROM {VIEW}
 WHERE resort = :resort
   AND r_type = 'R'
   AND resv_status IN ('RESERVED', 'CHECKED IN', 'CHECKED OUT', 'CANCELLED')
   AND business_date >= ADD_MONTHS(TRUNC(SYSDATE, 'YYYY'), -13)
   AND business_date <  ADD_MONTHS(TRUNC(SYSDATE, 'YYYY'), 24)
 GROUP BY TRUNC(business_date), TRUNC(booked_date), TRUNC(cancellation_date),
          NVL(source_code, 'Direct'),
          CASE WHEN resv_status IN ('RESERVED', 'CHECKED IN') THEN 'A'
               WHEN resv_status = 'CHECKED OUT'                THEN 'D'
               ELSE 'C' END,
          CASE WHEN NVL(room_revenue, 0) > 0 THEN 1 ELSE 0 END
"""
