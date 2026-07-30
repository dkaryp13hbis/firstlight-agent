/* ============================================================================
   logis_test_from_procedures_hotel2.sql
   Revenue (logis) test derived DIRECTLY from each stored procedure's SELECT.
   No INSERT / DELETE / UPDATE — read-only. mpehotel = 2.

   Each script:
     - sets the same params the proc receives (@mpeHotel, @Datvon, @Datbis)
     - reproduces the proc's derived boundaries (@PDate/@DatvonFut/@DatbisHist)
     - rewrites the @SharesTable cursor as a CTE (only pure-SELECT change)
     - keeps every JOIN/WHERE that affects logis or row count; drops the
       lookup LEFT JOINs (kunden/natcode/vip/…) that cannot change the sum
     - aggregates logis for 2025 stay-dates up to today-365, active only.

   Run each batch (separated by GO) on its own.
   ============================================================================ */


/* ##########################################################################
   SCRIPT 1 — FillBIHitIA  (hitia)   ->  bidata.proteluser.hitia
   ########################################################################## */
DECLARE @mpeHotel INT      = 2;
DECLARE @Datvon   DATETIME = '2025-01-01';
DECLARE @Datbis   DATETIME = CAST(GETDATE() AS DATE);   -- today: lets the history leg cover all of 2025
DECLARE @ly_start DATE     = '2025-01-01';
DECLARE @ly_end   DATE     = DATEADD(YEAR, -1, CAST(GETDATE() AS DATE));   -- today - 365d

DECLARE @PDate DATETIME, @DatvonFut DATETIME, @DatbisHist DATETIME;
SELECT @PDate      = d.pdate,
       @DatvonFut  = CASE WHEN @Datvon > d.pdate THEN @Datvon ELSE d.pdate END,
       @DatbisHist = CASE WHEN @Datbis < d.pdate THEN @Datbis ELSE DATEADD(DAY,-1,d.pdate) END
FROM protel.proteluser.datum d
WHERE d.mpehotel = @mpeHotel;

;WITH shares AS (   -- rewrite of @Shares/@SharesTable cursor (FillBIHitIA)
    SELECT b.buchnr, b.leistacc, b.sharenr, b.datumvon AS datumVon, b.datumbis AS datumBis
    FROM protel.proteluser.buch b
    WHERE b.leistacc <> b.sharenr
      AND b.sharenr IN (SELECT DISTINCT b2.sharenr FROM protel.proteluser.buch b2
                        WHERE b2.sharenr > 0 AND b2.mpehotel = @mpeHotel
                          AND b2.datumvon <= @Datbis AND b2.datumbis >= @Datvon)
      AND EXISTS (SELECT 1 FROM protel.proteluser.buch bh
                  WHERE bh.sharenr = b.sharenr AND bh.leistacc = bh.sharenr AND bh.zimmernr = b.zimmernr)
),
proc_rows AS (

    /* ---- FUTURE / on-the-books leg: prfuture + buch ---- */
    SELECT
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), prfuture.datum, 120)) AS [date],
        ISNULL(buch.reschar, -1)                                       AS reschar,
        ISNULL(k.katnr, -1)                                            AS katnr,
        ISNULL(CASE WHEN k.zimmer = 0          THEN 0
                    WHEN st.buchnr IS NOT NULL THEN 0
                    WHEN buch.datumvon <= prfuture.datum AND buch.datumbis > prfuture.datum
                     AND buch.reschar IN (0,1,4,5) THEN buch.anzahl ELSE 0 END, 0) AS Occupancy,
        ISNULL(prfuture.logis * buch.anzahl, 0)                        AS logis     -- <== hitia future logis
    FROM protel.proteluser.prfuture prfuture
    INNER JOIN protel.proteluser.buch buch
           ON prfuture.buchnr = buch.buchnr
          AND (prfuture.datum < buch.datumbis OR prfuture.datum = buch.globdbis)
          AND buch.datumvon < buch.datumbis
    INNER JOIN protel.proteluser.kat k ON buch.katnr = k.katnr
    LEFT  JOIN shares st ON st.buchnr = buch.buchnr AND st.leistacc = buch.leistacc
                        AND st.sharenr = buch.sharenr
                        AND prfuture.datum BETWEEN st.datumVon AND st.datumBis
    WHERE @Datbis >= @PDate
      AND prfuture.stationid = -10
      AND prfuture.mpehotel  = @mpeHotel
      AND prfuture.datum BETWEEN @DatvonFut AND @Datbis
      AND (prfuture.datum < buch.datumbis OR prfuture.datum = buch.globdbis)

    UNION ALL

    /* ---- HISTORY / actuals leg: dayhist_rev + stobuch ---- */
    SELECT
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), dayhi.datum, 120))    AS [date],
        ISNULL(dayhi.reschar, 0)                                      AS reschar,
        ISNULL(dayhi.katnr, -1)                                       AS katnr,
        CASE WHEN dayhi.kattyp = 0 AND dayhi.cntroom = 1 AND dayhi.rmdepart = 0 THEN 1 ELSE 0 END AS Occupancy,
        CASE WHEN sto.buchnr IS NOT NULL
              AND ISNULL(dayhi.datum,'1900-01-01') <> sto.datumbis
             THEN ISNULL(sto.preis, 0)                                             -- cancelled night -> stobuch.preis
             ELSE ISNULL(dayhi.o_logis, 0) END                        AS logis     -- <== hitia history logis
    FROM protel.proteluser.dayhist_rev dayhi
    LEFT JOIN protel.proteluser.stobuch sto ON sto.buchnr = dayhi.leistacc
    WHERE @Datvon < @PDate
      AND dayhi.mpehotel = @mpeHotel
      AND dayhi.datum BETWEEN @Datvon AND @DatbisHist
)
SELECT
    MONTH([date])    AS stay_month,
    COUNT(*)         AS rows_cnt,
    SUM(Occupancy)   AS room_nights,
    SUM(logis)       AS rev_logis
FROM proc_rows
WHERE reschar < 2
  AND [date] BETWEEN @ly_start AND @ly_end
  AND katnr NOT IN (SELECT katnr FROM protel.proteluser.kat WHERE zimmer = 0)
GROUP BY MONTH([date]) WITH ROLLUP
ORDER BY GROUPING(MONTH([date])), MONTH([date]);
GO


/* ##########################################################################
   SCRIPT 2 — Fillhitia_2  (proteldata)  ->  bidata.dbo.proteldata
   Reproduces the two revenue legs of Fillhitia_2 (active prfuture+buch, and
   cancellations prfuture+stobuch). The out-of-order/block leg is omitted:
   its logis is a literal 0 and cannot affect the sum.

   NOTE 1 (structural): every revenue row keys off prfuture.datum, and the
   filter is prfuture.datum BETWEEN @DatvonFut (= pdate) AND @Datbis, under the
   IF (@Datbis >= @PDate) gate. There is NO dayhist_rev history leg in the
   procedure text supplied. => for 2025 stay-dates this returns (near) ZERO by
   construction: proteldata cannot report same-time-last-year revenue.
   NOTE 2: proc text was truncated inside the stobuch leg; its WHERE is assumed
   identical to the active leg (stationid=-10, mpehotel, datum BETWEEN Fut/Bis).
   ########################################################################## */
DECLARE @mpeHotel INT      = 2;
DECLARE @Datvon   DATETIME = '2025-01-01';
DECLARE @Datbis   DATETIME = CAST(GETDATE() AS DATE);
DECLARE @ly_start DATE     = '2025-01-01';
DECLARE @ly_end   DATE     = DATEADD(YEAR, -1, CAST(GETDATE() AS DATE));

DECLARE @PDate DATETIME, @DatvonFut DATETIME;
SELECT @PDate     = d.pdate,
       @DatvonFut = CASE WHEN @Datvon > d.pdate THEN @Datvon ELSE d.pdate END
FROM protel.proteluser.datum d
WHERE d.mpehotel = @mpeHotel;

;WITH sharenrs AS (   -- @Shares population (Fillhitia_2): rooms shared by >1 reservation
    SELECT DISTINCT fin.sharenr
    FROM (
        SELECT b.sharenr, b.zimmernr, COUNT(DISTINCT b.leistacc) AS leistacc
        FROM (
            SELECT b.sharenr, b.zimmernr
            FROM protel.proteluser.buch AS b
            INNER JOIN protel.proteluser.kat AS k ON k.katnr = b.katnr
            WHERE b.sharenr > 0 AND b.globdvon <= @Datbis AND b.globdbis > @Datvon
              AND b.mpehotel = @mpeHotel
              AND ((b.globbnr < 1) OR (b.globbnr > 0 AND b.umzdurch = 1))
            GROUP BY b.sharenr, b.zimmernr
        ) a
        INNER JOIN protel.proteluser.buch AS b
               ON b.sharenr = a.sharenr AND b.zimmernr = a.zimmernr
              AND ((b.globbnr < 1) OR (b.globbnr > 0 AND b.umzdurch = 1))
        GROUP BY b.sharenr, b.zimmernr
    ) fin
    WHERE fin.leistacc > 1
),
shares AS (           -- @SharesTable population
    SELECT DISTINCT b.buchnr, b.leistacc, b.sharenr, b.datumvon AS datumVon, b.datumbis AS datumBis
    FROM protel.proteluser.buch b
    INNER JOIN sharenrs s ON s.sharenr = b.sharenr
    LEFT  JOIN protel.proteluser.buch bh
           ON bh.sharenr = s.sharenr AND bh.leistacc = bh.sharenr AND bh.zimmernr = b.zimmernr
    WHERE b.leistacc <> b.sharenr AND bh.buchnr IS NOT NULL
),
proc_rows AS (

    /* ---- ACTIVE leg: prfuture + buch ---- */
    SELECT
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), prfuture.datum, 120)) AS [date],
        ISNULL(buch.reschar, -1)                                       AS reschar,
        ISNULL(k.katnr, -1)                                            AS katnr,
        ISNULL(CASE WHEN buch.datumvon <= prfuture.datum AND buch.datumbis > prfuture.datum
                     AND st.buchnr IS NULL AND buch.reschar IN (0,1,2,4,5) THEN buch.anzahl ELSE 0 END, 0) AS Occupancy,
        ISNULL(prfuture.logis * buch.anzahl, 0)                        AS logis     -- <== proteldata active logis
    FROM protel.proteluser.prfuture prfuture
    INNER JOIN protel.proteluser.buch buch
           ON prfuture.buchnr = buch.buchnr
          AND (prfuture.datum < buch.datumbis OR prfuture.datum = buch.globdbis)
          AND buch.datumvon < buch.datumbis
    LEFT  JOIN shares st ON st.buchnr = buch.buchnr AND st.leistacc = buch.leistacc
                        AND st.sharenr = buch.sharenr
                        AND prfuture.datum BETWEEN st.datumVon AND st.datumBis
    INNER JOIN protel.proteluser.kat k ON buch.katnr = k.katnr
    WHERE @Datbis >= @PDate
      AND prfuture.stationid = -10
      AND prfuture.mpehotel  = @mpeHotel
      AND prfuture.datum BETWEEN @DatvonFut AND @Datbis
      AND (prfuture.datum < buch.datumbis OR prfuture.datum = buch.globdbis)

    UNION ALL

    /* ---- CANCELLATIONS leg: prfuture + stobuch  (logis STILL from prfuture) ---- */
    SELECT
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), prfuture.datum, 120)) AS [date],
        ISNULL(buch.reschar, -1)                                       AS reschar,
        ISNULL(k.katnr, -1)                                            AS katnr,
        ISNULL(CASE WHEN buch.datumvon <= prfuture.datum AND buch.datumbis > prfuture.datum
                     AND st.buchnr IS NULL AND buch.reschar IN (0,1,2,4,5) THEN buch.anzahl ELSE 0 END, 0) AS Occupancy,
        ISNULL(prfuture.logis * buch.anzahl, 0)                        AS logis     -- <== proteldata cancel logis (prfuture, not stobuch.preis)
    FROM protel.proteluser.prfuture prfuture
    INNER JOIN protel.proteluser.stobuch buch
           ON prfuture.buchnr = buch.buchnr
          AND (prfuture.datum < buch.datumbis OR prfuture.datum = buch.datumbis)
          AND buch.datumvon < buch.datumbis
    LEFT  JOIN shares st ON st.buchnr = buch.buchnr AND st.leistacc = buch.buchnr
                        AND st.sharenr = buch.sharenr
                        AND prfuture.datum BETWEEN st.datumVon AND st.datumBis
    INNER JOIN protel.proteluser.kat k ON buch.katnr = k.katnr
    WHERE @Datbis >= @PDate
      AND prfuture.stationid = -10
      AND prfuture.mpehotel  = @mpeHotel
      AND prfuture.datum BETWEEN @DatvonFut AND @Datbis
)
SELECT
    MONTH([date])    AS stay_month,
    COUNT(*)         AS rows_cnt,
    SUM(Occupancy)   AS room_nights,
    SUM(logis)       AS rev_logis
FROM proc_rows
WHERE reschar < 2
  AND [date] BETWEEN @ly_start AND @ly_end
  AND katnr NOT IN (SELECT katnr FROM protel.proteluser.kat WHERE zimmer = 0)
GROUP BY MONTH([date]) WITH ROLLUP
ORDER BY GROUPING(MONTH([date])), MONTH([date]);
GO


/* ##########################################################################
   DIAGNOSTIC — which years does the LIVE proteldata table actually hold?
   Run this against the reseller's real table to confirm NOTE 1 above.
   ########################################################################## */
-- SELECT YEAR([date]) AS stay_year, COUNT(*) AS rows_cnt, SUM(logis) AS rev_logis
-- FROM bidata.dbo.proteldata
-- WHERE mpehotel = 2 AND reschar < 2
-- GROUP BY YEAR([date]) ORDER BY stay_year;
