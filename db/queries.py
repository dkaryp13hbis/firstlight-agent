"""
All SQL queries against the Protel PMS (bidata + protel databases).

Cancellation logic (mirrors the Power BI model):
  - Cancellations_In:  original booking record, positive room nights, original SystemDate
  - Cancellations_Out: cancellation event, negative room nights, Canceled date as book date
  Used only for pickup queries (book-date axis). Stay-date queries use Status < 2 only.

Fake room types: categories in protel.proteluser.kat where zimmer = 0
(virtual/package categories with no physical rooms).
"""

# ------------------------------------------------------------------
# Shared fragments
# ------------------------------------------------------------------

_FAKE_RT_EXCLUDE = """
    h.katnr NOT IN (
        SELECT katnr FROM protel.proteluser.kat WHERE zimmer = 0
    )
"""

# ------------------------------------------------------------------
# Q1: Yesterday + MTD KPIs  (stay-date axis, active only)
# ------------------------------------------------------------------

Q_KPIS = """
DECLARE @today        DATE = CAST(GETDATE() AS DATE);
DECLARE @yesterday    DATE = DATEADD(DAY, -1, @today);
DECLARE @yday_ly      DATE = DATEADD(YEAR, -1, @yesterday);
DECLARE @stly_cap     DATE = DATEADD(YEAR, -1, @today);
DECLARE @mtd_start    DATE = DATEFROMPARTS(YEAR(@yesterday), MONTH(@yesterday), 1);
DECLARE @mtd_start_ly DATE = DATEADD(YEAR, -1, @mtd_start);

WITH kpi_rows AS (
    -- TY: active bookings only
    SELECT h.date, h.logis, h.Occupancy, 'TY' AS period
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar < 2
      AND {fake_rt}
      AND (h.date = @yesterday OR h.date BETWEEN @mtd_start AND @yesterday)

    UNION ALL

    -- LY yesterday + MTD: active + cancelled after today's date last year
    -- Occupancy is 0 on cancelled records in Protel, so restore to 1 per row
    SELECT h.date, h.logis,
           CASE WHEN h.reschar < 2 THEN h.Occupancy WHEN CAST(h.datumbis AS DATE) = CAST(h.date AS DATE) THEN 0 ELSE 1 END AS Occupancy,
           'LY' AS period
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND (h.reschar < 2 OR (h.reschar = 2 AND CAST(h.Canceled AS DATE) > @stly_cap))
      AND {fake_rt}
      AND (h.date = @yday_ly OR h.date BETWEEN @mtd_start_ly AND @yday_ly)

    UNION ALL

    -- STLY full year by month: all of last year booked by same date last year
    SELECT h.date, h.logis,
           CASE WHEN h.reschar < 2 THEN h.Occupancy WHEN CAST(h.datumbis AS DATE) = CAST(h.date AS DATE) THEN 0 ELSE 1 END AS Occupancy,
           'STLY' AS period
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND (h.reschar < 2 OR (h.reschar = 2 AND CAST(h.Canceled AS DATE) > @stly_cap))
      AND {fake_rt}
      AND YEAR(h.date) = YEAR(@stly_cap)
      AND CAST(h.SystemDate AS DATE) <= @stly_cap
)
SELECT
    SUM(CASE WHEN period = 'TY'   AND date = @yesterday                         THEN logis     ELSE 0 END) AS rev_yday_ty,
    SUM(CASE WHEN period = 'TY'   AND date = @yesterday                         THEN Occupancy ELSE 0 END) AS rn_yday_ty,
    SUM(CASE WHEN period = 'LY'   AND date = @yday_ly                           THEN logis     ELSE 0 END) AS rev_yday_ly,
    SUM(CASE WHEN period = 'LY'   AND date = @yday_ly                           THEN Occupancy ELSE 0 END) AS rn_yday_ly,
    SUM(CASE WHEN period = 'TY'   AND date BETWEEN @mtd_start    AND @yesterday THEN logis     ELSE 0 END) AS rev_mtd_ty,
    SUM(CASE WHEN period = 'TY'   AND date BETWEEN @mtd_start    AND @yesterday THEN Occupancy ELSE 0 END) AS rn_mtd_ty,
    SUM(CASE WHEN period = 'LY'   AND date BETWEEN @mtd_start_ly AND @yday_ly   THEN logis     ELSE 0 END) AS rev_mtd_ly,
    SUM(CASE WHEN period = 'LY'   AND date BETWEEN @mtd_start_ly AND @yday_ly   THEN Occupancy ELSE 0 END) AS rn_mtd_ly,
    -- Monthly STLY room nights (occupancy = rn / inventory_for_month)
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 1  THEN Occupancy ELSE 0 END) AS rn_stly_1,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 2  THEN Occupancy ELSE 0 END) AS rn_stly_2,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 3  THEN Occupancy ELSE 0 END) AS rn_stly_3,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 4  THEN Occupancy ELSE 0 END) AS rn_stly_4,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 5  THEN Occupancy ELSE 0 END) AS rn_stly_5,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 6  THEN Occupancy ELSE 0 END) AS rn_stly_6,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 7  THEN Occupancy ELSE 0 END) AS rn_stly_7,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 8  THEN Occupancy ELSE 0 END) AS rn_stly_8,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 9  THEN Occupancy ELSE 0 END) AS rn_stly_9,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 10 THEN Occupancy ELSE 0 END) AS rn_stly_10,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 11 THEN Occupancy ELSE 0 END) AS rn_stly_11,
    SUM(CASE WHEN period = 'STLY' AND MONTH(date) = 12 THEN Occupancy ELSE 0 END) AS rn_stly_12
FROM kpi_rows;
""".format(fake_rt=_FAKE_RT_EXCLUDE)


# ------------------------------------------------------------------
# Q2: Yesterday in-house snapshot  (reservation level, not stay-day)
# ------------------------------------------------------------------

Q_INHOUSE = """
DECLARE @yesterday DATE = DATEADD(DAY, -1, CAST(GETDATE() AS DATE));

SELECT
    SUM(CASE WHEN CAST(r.datumvon AS DATE) = @yesterday                                     THEN 1 ELSE 0 END) AS arrivals,
    SUM(CASE WHEN CAST(r.datumbis AS DATE) = @yesterday                                     THEN 1 ELSE 0 END) AS departures,
    SUM(CASE WHEN CAST(r.datumvon AS DATE) < @yesterday AND CAST(r.datumbis AS DATE) > @yesterday THEN 1 ELSE 0 END) AS stayovers
FROM (
    SELECT DISTINCT reser, datumvon, datumbis
    FROM bidata.proteluser.Hitia
    WHERE mpehotel = ?
      AND reschar < 2
      AND {fake_rt}
      AND (
          CAST(datumvon AS DATE) = @yesterday
          OR CAST(datumbis AS DATE) = @yesterday
          OR (CAST(datumvon AS DATE) < @yesterday AND CAST(datumbis AS DATE) > @yesterday)
      )
) r;
""".format(fake_rt=_FAKE_RT_EXCLUDE.replace("h.", ""))


# ------------------------------------------------------------------
# Q3: Pickup — last 24h and last 7d  (book-date axis, UNION approach)
# New bookings and cancellations are shown separately.
# ------------------------------------------------------------------

Q_PICKUP = """
DECLARE @today         DATE = CAST(GETDATE() AS DATE);
DECLARE @yesterday     DATE = DATEADD(DAY, -1, @today);
DECLARE @three_ago     DATE = DATEADD(DAY, -3, @today);
DECLARE @seven_ago     DATE = DATEADD(DAY, -7, @today);

WITH new_bookings AS (
    SELECT
        CAST(SystemDate AS DATE) AS book_date,
        MONTH(date)              AS stay_month,
        Occupancy                AS room_nights,
        logis                    AS revenue
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar < 2
      AND {fake_rt}
      AND h.date > @today
      AND CAST(h.SystemDate AS DATE) >= @seven_ago
),

cancellations AS (
    SELECT
        CAST(h.Canceled AS DATE)                             AS cancel_date,
        MONTH(h.date)                                        AS stay_month,
        CASE WHEN h.datumbis = h.date THEN 0 ELSE 1 END     AS room_nights,
        h.logis                                              AS revenue
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar = 2
      AND {fake_rt}
      AND h.date > @today
      AND CAST(h.Canceled AS DATE) >= @seven_ago
)

SELECT
    -- Last 1 day
    SUM(CASE WHEN b.book_date = @yesterday  THEN b.room_nights ELSE 0 END) AS pickup_1d_rn,
    SUM(CASE WHEN b.book_date = @yesterday  THEN b.revenue     ELSE 0 END) AS pickup_1d_rev,
    -- Last 3 days
    SUM(CASE WHEN b.book_date >= @three_ago THEN b.room_nights ELSE 0 END) AS pickup_3d_rn,
    SUM(CASE WHEN b.book_date >= @three_ago THEN b.revenue     ELSE 0 END) AS pickup_3d_rev,
    -- Last 7 days
    SUM(b.room_nights)                                                       AS pickup_7d_rn,
    SUM(b.revenue)                                                           AS pickup_7d_rev,
    -- Cancellations 1 day
    (SELECT COUNT(*)     FROM cancellations WHERE cancel_date = @yesterday)  AS cancel_1d_count,
    (SELECT SUM(revenue) FROM cancellations WHERE cancel_date = @yesterday)  AS cancel_1d_rev,
    -- Cancellations 3 days
    (SELECT COUNT(*)     FROM cancellations WHERE cancel_date >= @three_ago) AS cancel_3d_count,
    (SELECT SUM(revenue) FROM cancellations WHERE cancel_date >= @three_ago) AS cancel_3d_rev,
    -- Cancellations 7 days
    (SELECT COUNT(*)     FROM cancellations)                                 AS cancel_7d_count,
    (SELECT SUM(revenue) FROM cancellations)                                 AS cancel_7d_rev,
    -- Top pickup month (7-day window)
    (SELECT TOP 1 stay_month       FROM new_bookings GROUP BY stay_month ORDER BY SUM(room_nights) DESC) AS top_month,
    (SELECT TOP 1 SUM(room_nights) FROM new_bookings GROUP BY stay_month ORDER BY SUM(room_nights) DESC) AS top_month_rn
FROM new_bookings b;
""".format(fake_rt=_FAKE_RT_EXCLUDE)


# ------------------------------------------------------------------
# Q4: OTB pace by month — full year (stay-date axis, active only)
# TY = all 2026 stays (past months = final actuals, future = OTB)
# STLY = all 2025 stays booked by same date last year
# Final LY = all 12 months of 2025, no book-date cap
# ------------------------------------------------------------------

Q_PACE = """
DECLARE @today     DATE = CAST(GETDATE() AS DATE);
DECLARE @stly_cap  DATE = DATEADD(YEAR, -1, @today);   -- book date cap for STLY

-- Group by month only (TY=2026 and LY=2025 use same month numbers, different years).
SELECT
    stay_month,
    SUM(rn_ty)    AS rn_otb_ty,
    SUM(rn_stly)  AS rn_stly,
    SUM(rn_fly)   AS rn_final_ly,
    SUM(rev_ty)   AS rev_otb_ty,
    SUM(rev_stly) AS rev_stly,
    SUM(rev_fly)  AS rev_final_ly
FROM (
    -- OTB TY: all active bookings for this full year (past months = final, future = OTB)
    SELECT
        MONTH(h.date) AS stay_month,
        h.Occupancy   AS rn_ty,
        0             AS rn_stly,
        0             AS rn_fly,
        h.logis       AS rev_ty,
        0.0           AS rev_stly,
        0.0           AS rev_fly
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar < 2
      AND {fake_rt}
      AND YEAR(h.date) = YEAR(@today)

    UNION ALL

    -- STLY: all of last year booked by same date last year (full-year comparison at same booking stage)
    -- Occupancy is 0 on cancelled records in Protel, so restore to 1 per row
    SELECT
        MONTH(h.date),
        0,
        CASE WHEN h.reschar < 2 THEN h.Occupancy WHEN CAST(h.datumbis AS DATE) = CAST(h.date AS DATE) THEN 0 ELSE 1 END,
        0,
        0.0,
        h.logis,
        0.0
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND (h.reschar < 2 OR (h.reschar = 2 AND CAST(h.Canceled AS DATE) > @stly_cap))
      AND {fake_rt}
      AND YEAR(h.date) = YEAR(@stly_cap)
      AND CAST(h.SystemDate AS DATE) <= @stly_cap

    UNION ALL

    -- Final LY: all 12 months of last year, no book-date cap
    SELECT
        MONTH(h.date),
        0,
        0,
        h.Occupancy,
        0.0,
        0.0,
        h.logis
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar < 2
      AND {fake_rt}
      AND YEAR(h.date) = YEAR(@stly_cap)
) t
GROUP BY stay_month
ORDER BY stay_month;
""".format(fake_rt=_FAKE_RT_EXCLUDE)


# ------------------------------------------------------------------
# Q5: Top sources OTB — full-year revenue vs STLY  (book-date axis, active only)
# ------------------------------------------------------------------

Q_SOURCES_OTB = """
DECLARE @today    DATE = CAST(GETDATE() AS DATE);
DECLARE @stly_cap DATE = DATEADD(YEAR, -1, @today);
DECLARE @ty_year  INT  = YEAR(@today);
DECLARE @ly_year  INT  = YEAR(@today) - 1;

SELECT
    source,
    SUM(CASE WHEN period = 'TY' THEN rev  ELSE 0 END) AS rev_ty,
    SUM(CASE WHEN period = 'TY' THEN rn   ELSE 0 END) AS rn_ty,
    SUM(CASE WHEN period = 'LY' THEN rev  ELSE 0 END) AS rev_stly,
    SUM(CASE WHEN period = 'LY' THEN rn   ELSE 0 END) AS rn_stly
FROM (
    SELECT ISNULL(NULLIF(LTRIM(RTRIM(h.Sourcen)), ''), 'Direct') AS source,
           'TY' AS period, h.logis AS rev, h.Occupancy AS rn
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar < 2
      AND {fake_rt}
      AND YEAR(h.date) = @ty_year

    UNION ALL

    SELECT ISNULL(NULLIF(LTRIM(RTRIM(h.Sourcen)), ''), 'Direct'),
           'LY', h.logis, h.Occupancy
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND (h.reschar < 2 OR (h.reschar = 2 AND CAST(h.Canceled AS DATE) > @stly_cap))
      AND {fake_rt}
      AND YEAR(h.date) = @ly_year
      AND CAST(h.SystemDate AS DATE) <= @stly_cap
) t
GROUP BY source
ORDER BY SUM(CASE WHEN period = 'TY' THEN rev ELSE 0 END) DESC;
""".format(fake_rt=_FAKE_RT_EXCLUDE)


# ------------------------------------------------------------------
# Q6: Next 7 days OTB  (day by day, active only)
# ------------------------------------------------------------------

Q_NEXT7 = """
DECLARE @today DATE = CAST(GETDATE() AS DATE);

SELECT
    h.date                                                                AS stay_date,
    SUM(h.Occupancy)                                                      AS room_nights,
    SUM(h.logis)                                                          AS revenue,
    COUNT(DISTINCT CASE WHEN CAST(h.datumvon AS DATE) = h.date THEN h.reser END) AS arrivals
FROM bidata.proteluser.Hitia h
WHERE h.mpehotel = ?
  AND h.reschar < 2
  AND {fake_rt}
  AND h.date > @today
  AND h.date <= DATEADD(DAY, 7, @today)
GROUP BY h.date
ORDER BY h.date;
""".format(fake_rt=_FAKE_RT_EXCLUDE)


# ------------------------------------------------------------------
# Q7: Available inventory for yesterday + next 7 days + future months
# Total physical rooms per date (from operation dates x room counts)
# ------------------------------------------------------------------

Q_INVENTORY = """
DECLARE @today      DATE = CAST(GETDATE() AS DATE);
DECLARE @yesterday  DATE = DATEADD(DAY, -1, @today);
DECLARE @yday_ly    DATE = DATEADD(YEAR, -1, @yesterday);

-- Physical room count from zimmer (saison has no hotel filter column).
-- Same count applied to every requested date via CROSS JOIN.
SELECT
    d.ref_date,
    z.total_rooms
FROM (
    SELECT @yesterday        AS ref_date
    UNION SELECT @yday_ly
    UNION SELECT @today
    UNION SELECT DATEADD(DAY,1,@today)
    UNION SELECT DATEADD(DAY,2,@today)
    UNION SELECT DATEADD(DAY,3,@today)
    UNION SELECT DATEADD(DAY,4,@today)
    UNION SELECT DATEADD(DAY,5,@today)
    UNION SELECT DATEADD(DAY,6,@today)
    UNION SELECT DATEADD(DAY,7,@today)
) d
CROSS JOIN (
    SELECT COUNT(*) AS total_rooms
    FROM protel.proteluser.zimmer
    WHERE mpehotel = ?
      AND kat NOT IN (SELECT katnr FROM protel.proteluser.kat WHERE zimmer = 0)
) z;
""".format()


# ------------------------------------------------------------------
# Q8: Booking Revenue Curves
# Returns cumulative revenue by book-month for TY and LY,
# for current month, next month, and all future months (full-year view).
# Book dates can span multiple years (long lead times).
# ------------------------------------------------------------------

Q_BOOKING_CURVE = """
DECLARE @today    DATE = CAST(GETDATE() AS DATE);
DECLARE @ty_year  INT  = YEAR(@today);
DECLARE @ly_year  INT  = YEAR(@today) - 1;
DECLARE @stay_m1  INT  = MONTH(@today);
DECLARE @stay_m2  INT  = CASE WHEN MONTH(@today) < 12 THEN MONTH(@today) + 1 ELSE 1 END;
DECLARE @m2_year  INT  = CASE WHEN MONTH(@today) < 12 THEN @ty_year ELSE @ty_year + 1 END;
DECLARE @ly_m2yr  INT  = CASE WHEN @stay_m2 = 1 THEN @ly_year + 1 ELSE @ly_year END;

-- X axis = days before end-of-stay-month, bucketed at 30d intervals, capped 0-200.
-- Bookings made >200d out are folded into the 200 bucket so curves start from real revenue.
SELECT stay_month, stay_year, period,
       CASE
           WHEN raw_days < 0   THEN 0
           WHEN raw_days > 200 THEN 200
           ELSE (raw_days / 30) * 30
       END AS days_bucket,
       SUM(revenue) AS revenue
FROM (
    SELECT MONTH(h.date) AS stay_month, YEAR(h.date) AS stay_year, 'TY' AS period,
           DATEDIFF(DAY, CAST(h.SystemDate AS DATE), EOMONTH(h.date)) AS raw_days,
           h.logis AS revenue
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ? AND h.reschar < 2 AND {fake_rt}
      AND MONTH(h.date) = @stay_m1 AND YEAR(h.date) = @ty_year

    UNION ALL

    SELECT MONTH(h.date), YEAR(h.date), 'TY',
           DATEDIFF(DAY, CAST(h.SystemDate AS DATE), EOMONTH(h.date)), h.logis
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ? AND h.reschar < 2 AND {fake_rt}
      AND MONTH(h.date) = @stay_m2 AND YEAR(h.date) = @m2_year

    UNION ALL

    SELECT MONTH(h.date), YEAR(h.date), 'LY',
           DATEDIFF(DAY, CAST(h.SystemDate AS DATE), EOMONTH(h.date)), h.logis
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ? AND h.reschar < 2 AND {fake_rt}
      AND MONTH(h.date) = @stay_m1 AND YEAR(h.date) = @ly_year

    UNION ALL

    SELECT MONTH(h.date), YEAR(h.date), 'LY',
           DATEDIFF(DAY, CAST(h.SystemDate AS DATE), EOMONTH(h.date)), h.logis
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ? AND h.reschar < 2 AND {fake_rt}
      AND MONTH(h.date) = @stay_m2 AND YEAR(h.date) = @ly_m2yr
) t
GROUP BY stay_month, stay_year, period,
         CASE WHEN raw_days < 0 THEN 0 WHEN raw_days > 200 THEN 200 ELSE (raw_days/30)*30 END
ORDER BY stay_year, stay_month, period, days_bucket DESC;
""".format(fake_rt=_FAKE_RT_EXCLUDE)


Q_BOOKING_CURVE_FULL_MONTHS = """
DECLARE @today    DATE = CAST(GETDATE() AS DATE);
DECLARE @stly_cap DATE = DATEADD(YEAR, -1, @today);
DECLARE @ty_year  INT  = YEAR(@today);
DECLARE @ly_year  INT  = YEAR(@today) - 1;

SELECT book_month, period, SUM(revenue) AS revenue
FROM (
    SELECT MONTH(CAST(h.SystemDate AS DATE)) AS book_month,
           'TY' AS period,
           h.logis AS revenue
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar < 2
      AND {fake_rt}
      AND YEAR(h.date) = @ty_year
      AND CAST(h.SystemDate AS DATE) <= @today

    UNION ALL

    SELECT MONTH(CAST(h.SystemDate AS DATE)),
           'LY',
           h.logis
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = ?
      AND h.reschar < 2
      AND {fake_rt}
      AND YEAR(h.date) = @ly_year
) t
GROUP BY book_month, period
ORDER BY period, book_month;
""".format(fake_rt=_FAKE_RT_EXCLUDE)
