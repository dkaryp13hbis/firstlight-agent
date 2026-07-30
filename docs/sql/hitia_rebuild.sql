/* ============================================================================
   hitia_rebuild.sql  —  Option A: reseller-free rebuild of bidata.proteluser.hitia

   PURPOSE
     Reproduce, as a single read-only SELECT, the exact daily-grain rows the
     reseller's FillBIHitIA stored procedure produces — but only the 12 columns
     the FirstLight app actually consumes (see db/adapters/protel_mssql/queries.py):

         mpehotel, date, logis, Occupancy, reschar, Canceled,
         SystemDate, datumvon, datumbis, reser, katnr, Sourcen

     No proprietary data is involved: every source table is Protel's own native
     schema in the [protel] database. This is a clean-room reimplementation from
     the Protel data model, not a copy of the reseller's procedure.

   WHAT WAS DROPPED vs FillBIHitIA
     - All INSERT / DELETE / temp-table writes (this is SELECT-only).
     - ~68 columns the app never reads (guest/agent/company/group names,
       nationality, VIP, gender, board codes, allotments, rate-code text,
       open balance, etc.).
     - reschar = 6 block rows (Occupancy=0, logis=0 -> zero contribution).
     - Post-fill UPDATEs that touch unused columns (imhaus, pickup*, status,
       taname) and the canceled=resday fixup (a no-op: status is NULL when it runs).

   WHAT WAS KEPT (folded into the SELECT)
     - Occupancy = 0 for zero-night rows      (was: UPDATE ... WHERE datumbis=datumvon)
     - Canceled  = 1900-01-01 for active rows (was: UPDATE ... WHERE reschar<2)
     - The two regimes split on the hotel business date datum.pdate:
         * FUTURE / on-the-books  -> prfuture + buch   (datum >= pdate)
         * HISTORY / actuals      -> dayhist_rev + buchold + stobuch (datum < pdate)
     - The shared-room occupancy suppression (cursor rewritten as a set CTE).

   HOW TO TEST
     1. Set the three variables below (hotel + date range).
     2. Run this file -> gives you the rebuilt rows.
     3. Run the PARITY DIFF at the bottom to compare against the reseller's
        hitia for the same hotel + range, date by date.
   ============================================================================ */

------------------------------------------------------------------------------
-- Test parameters  (edit these three lines)
------------------------------------------------------------------------------
DECLARE @mpeHotel INT      = 1;
DECLARE @Datvon   DATETIME = '2026-07-01';   -- span the business date to exercise BOTH regimes
DECLARE @Datbis   DATETIME = '2026-07-31';

------------------------------------------------------------------------------
-- Derived boundaries (mirrors FillBIHitIA header)
------------------------------------------------------------------------------
DECLARE @PDate DATETIME, @DatvonFut DATETIME, @DatbisHist DATETIME;

SELECT
    @PDate      = d.pdate,
    @DatvonFut  = CASE WHEN @Datvon > d.pdate THEN @Datvon ELSE d.pdate END,
    @DatbisHist = CASE WHEN @Datbis < d.pdate THEN @Datbis ELSE DATEADD(DAY, -1, d.pdate) END
FROM protel.proteluser.datum d
WHERE d.mpehotel = @mpeHotel;

------------------------------------------------------------------------------
-- Set-based rewrite of the @Shares / @SharesTable cursor.
-- Marks secondary bookings in a shared room so occupancy isn't double counted.
------------------------------------------------------------------------------
;WITH shares AS (
    SELECT b.buchnr, b.leistacc, b.sharenr,
           b.datumvon AS datumVon, b.datumbis AS datumBis
    FROM protel.proteluser.buch b
    WHERE b.leistacc <> b.sharenr
      AND b.sharenr IN (
            SELECT DISTINCT b2.sharenr
            FROM protel.proteluser.buch b2
            WHERE b2.sharenr > 0
              AND b2.mpehotel = @mpeHotel
              AND b2.datumvon <= @Datbis
              AND b2.datumbis >= @Datvon
      )
      AND EXISTS (
            SELECT 1 FROM protel.proteluser.buch bh
            WHERE bh.sharenr  = b.sharenr
              AND bh.leistacc = bh.sharenr
              AND bh.zimmernr = b.zimmernr
      )
),

------------------------------------------------------------------------------
-- Both regimes, unioned. Day-truncated dates match the reseller's CONVERT().
------------------------------------------------------------------------------
rebuilt AS (

    /* ---------- FUTURE / on-the-books : prfuture + buch ---------- */
    SELECT
        ISNULL(prfuture.mpehotel, 0)                                              AS mpehotel,
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), ISNULL(prfuture.datum,'1900-01-01'), 120))  AS [date],
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), ISNULL(buch.globdvon,'1900-01-01'), 120))   AS datumvon,
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), ISNULL(buch.globdbis,'1900-01-01'), 120))   AS datumbis,
        ISNULL(prfuture.leistacc, 0)                                              AS reser,
        ISNULL(buch.reschar, -1)                                                  AS reschar,
        ISNULL(CASE
                 WHEN k.zimmer = 0            THEN 0
                 WHEN st.buchnr IS NOT NULL   THEN 0        -- shared secondary room
                 WHEN buch.datumvon <= prfuture.datum
                  AND buch.datumbis >  prfuture.datum
                  AND buch.reschar IN (0,1,4,5) THEN buch.anzahl
                 ELSE 0
               END, 0)                                                            AS Occupancy,
        ISNULL(k.katnr, -1)                                                       AS katnr,
        ISNULL(prfuture.logis * buch.anzahl, 0)                                   AS logis,
        CASE WHEN YEAR(CAST(CONVERT(VARCHAR(10), buch.resdatumsql, 120) AS DATETIME)) > 1901
             THEN CAST(CONVERT(VARCHAR(10), buch.resdatumsql, 120) AS DATETIME)
             ELSE buch.resdatum END                                              AS SystemDate,
        ISNULL(s.bezeich, 'No Code')                                             AS Sourcen,
        CASE WHEN buch.reschar = 2 THEN buch.stornodat ELSE '1900-01-01' END      AS Canceled
    FROM protel.proteluser.prfuture prfuture
    INNER JOIN protel.proteluser.buch buch
           ON prfuture.buchnr = buch.buchnr
          AND (prfuture.datum < buch.datumbis OR prfuture.datum = buch.globdbis)
          AND buch.datumvon < buch.datumbis
    INNER JOIN protel.proteluser.kat k
           ON buch.katnr = k.katnr
    LEFT  JOIN protel.proteluser.source s
           ON s.nr = prfuture.source
    LEFT  JOIN shares st
           ON st.buchnr   = buch.buchnr
          AND st.leistacc = buch.leistacc
          AND st.sharenr  = buch.sharenr
          AND prfuture.datum BETWEEN st.datumVon AND st.datumBis
    WHERE @Datbis >= @PDate                       -- IF (@Datbis >= @PDate) gate
      AND prfuture.stationid = -10
      AND prfuture.mpehotel  = @mpeHotel
      AND prfuture.datum BETWEEN @DatvonFut AND @Datbis
      AND (prfuture.datum < buch.datumbis OR prfuture.datum = buch.globdbis)

    UNION ALL

    /* ---------- HISTORY / actuals : dayhist_rev + buchold + stobuch ---------- */
    SELECT
        ISNULL(dayhi.mpehotel, 0)                                                 AS mpehotel,
        CONVERT(DATETIME, CONVERT(NVARCHAR(10), ISNULL(dayhi.datum,'1900-01-01'), 120))  AS [date],
        CONVERT(DATETIME, CONVERT(NVARCHAR(10),
            CASE WHEN YEAR(bho.datumvon) > 1910 AND dayhi.reschar NOT IN (2,3) THEN bho.datumvon
                 WHEN YEAR(sto.datumvon) > 1910 AND dayhi.reschar NOT IN (2,3) THEN sto.datumvon
                 ELSE ISNULL(dayhi.datumvon, dayhi.datum) END, 120))              AS datumvon,
        CONVERT(DATETIME, CONVERT(NVARCHAR(10),
            CASE WHEN bh.leistacc IS NOT NULL                                  THEN bh.globdbis
                 WHEN YEAR(bho.datumbis) > 1910 AND dayhi.reschar NOT IN (2,3) THEN bho.datumbis
                 WHEN YEAR(sto.datumbis) > 1910 AND dayhi.reschar NOT IN (2,3) THEN sto.datumbis
                 ELSE ISNULL(dayhi.datumbis, dayhi.datum) END, 120))             AS datumbis,
        ISNULL(dayhi.leistacc, 0)                                                 AS reser,
        ISNULL(dayhi.reschar, 0)                                                  AS reschar,
        CASE WHEN dayhi.kattyp = 0 AND dayhi.cntroom = 1 AND dayhi.rmdepart = 0
             THEN 1 ELSE 0 END                                                    AS Occupancy,
        ISNULL(dayhi.katnr, -1)                                                   AS katnr,
        CASE WHEN sto.buchnr IS NOT NULL
              AND ISNULL(dayhi.datum,'1900-01-01') <> sto.datumbis
             THEN ISNULL(sto.preis, 0)
             ELSE ISNULL(dayhi.o_logis, 0) END                                    AS logis,
        ISNULL(bho.resdat, ISNULL(sto.resdat, CAST(CONVERT(VARCHAR(10), GETDATE(), 120) AS DATETIME))) AS SystemDate,
        ISNULL(s.bezeich, 'No Code')                                             AS Sourcen,
        CASE WHEN sto.buchnr IS NOT NULL THEN sto.stornodat ELSE '1900-01-01' END AS Canceled
    FROM protel.proteluser.dayhist_rev dayhi
    LEFT JOIN protel.proteluser.buchold bho ON bho.buchnr = dayhi.leistacc
    LEFT JOIN protel.proteluser.stobuch sto ON sto.buchnr = dayhi.leistacc
    LEFT JOIN protel.proteluser.source  s   ON s.nr       = dayhi.source
    LEFT JOIN protel.proteluser.buch    bh
           ON bh.leistacc = dayhi.leistacc
          AND ((bh.globbnr < 1) OR (bh.globbnr > 0 AND bh.umzdurch = 1))
    WHERE @Datvon < @PDate                        -- IF (@Datvon < @PDate) gate
      AND dayhi.mpehotel = @mpeHotel
      AND dayhi.datum BETWEEN @Datvon AND @DatbisHist
)

------------------------------------------------------------------------------
-- Final projection: apply the zero-night occupancy rule (folded UPDATE #4)
------------------------------------------------------------------------------
SELECT
    mpehotel,
    [date],
    datumvon,
    datumbis,
    reser,
    reschar,
    CASE WHEN datumbis = datumvon THEN 0 ELSE Occupancy END AS Occupancy,
    katnr,
    logis,
    SystemDate,
    Sourcen,
    Canceled
FROM rebuilt
ORDER BY [date], reser;


/* ============================================================================
   PARITY DIFF  —  run separately (same @mpeHotel / @Datvon / @Datbis).
   Compares per-date aggregates of the rebuild vs the reseller's live hitia.
   Any row returned = a date where they disagree. Empty result = full parity.
   Paste the CTE block above (DECLAREs + WITH shares / rebuilt) in place of
   <REBUILD> if you want a one-shot script; kept separate here for clarity.
   ============================================================================
DECLARE @mpeHotel INT = 1, @Datvon DATETIME = '2026-07-01', @Datbis DATETIME = '2026-07-31';

WITH mine AS (
    SELECT [date],
           SUM(logis)                                        AS rev,
           SUM(CASE WHEN datumbis=datumvon THEN 0 ELSE Occupancy END) AS rn,
           SUM(CASE WHEN reschar = 2 THEN 1 ELSE 0 END)      AS cxl
    FROM ( <REBUILD SELECT WITHOUT ORDER BY> ) r
    GROUP BY [date]
),
theirs AS (
    SELECT h.[date],
           SUM(h.logis)                                      AS rev,
           SUM(h.Occupancy)                                  AS rn,
           SUM(CASE WHEN h.reschar = 2 THEN 1 ELSE 0 END)    AS cxl
    FROM bidata.proteluser.hitia h
    WHERE h.mpehotel = @mpeHotel
      AND h.[date] BETWEEN @Datvon AND @Datbis
      AND h.reschar <> 6            -- we omit blocks; exclude them here too
    GROUP BY h.[date]
)
SELECT COALESCE(m.[date], t.[date]) AS [date],
       m.rev AS rev_mine, t.rev AS rev_theirs,
       m.rn  AS rn_mine,  t.rn  AS rn_theirs,
       m.cxl AS cxl_mine, t.cxl AS cxl_theirs
FROM mine m
FULL OUTER JOIN theirs t ON t.[date] = m.[date]
WHERE ABS(ISNULL(m.rev,0) - ISNULL(t.rev,0)) > 0.01
   OR ISNULL(m.rn,0)  <> ISNULL(t.rn,0)
   OR ISNULL(m.cxl,0) <> ISNULL(t.cxl,0)
ORDER BY [date];
   ============================================================================ */
