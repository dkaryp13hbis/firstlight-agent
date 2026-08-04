import sys, os
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from db.connection import get_connection

conn = get_connection()
cur = conn.cursor()
cur.execute("""
DECLARE @today DATE = CAST(GETDATE() AS DATE);

WITH new_bookings AS (
    SELECT
        CAST(h.SystemDate AS DATE) AS book_date,
        CASE WHEN h.reschar = 2
             THEN CASE WHEN h.datumbis = h.date THEN 0 ELSE 1 END
             ELSE h.Occupancy
        END AS room_nights,
        h.logis AS revenue,
        h.reser AS res_no
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = 1
      AND h.katnr NOT IN (SELECT katnr FROM protel.proteluser.kat WHERE zimmer = 0)
      AND CAST(h.SystemDate AS DATE) >= DATEADD(DAY, -7, @today)
      AND CAST(h.SystemDate AS DATE) <= @today
),
cancellations AS (
    SELECT
        CAST(h.Canceled AS DATE) AS cancel_date,
        CASE WHEN h.datumbis = h.date THEN 0 ELSE 1 END AS room_nights,
        h.logis AS revenue
    FROM bidata.proteluser.Hitia h
    WHERE h.mpehotel = 1
      AND h.reschar = 2
      AND h.katnr NOT IN (SELECT katnr FROM protel.proteluser.kat WHERE zimmer = 0)
      AND CAST(h.Canceled AS DATE) >= DATEADD(DAY, -7, @today)
      AND CAST(h.Canceled AS DATE) <= @today
)
SELECT
    b.book_date,
    COUNT(DISTINCT b.res_no)    AS reservations,
    SUM(b.room_nights)          AS booked_rn,
    ROUND(SUM(b.revenue), 0)    AS booked_rev,
    ISNULL((SELECT SUM(room_nights) FROM cancellations c WHERE c.cancel_date = b.book_date), 0) AS cancel_rn,
    ISNULL((SELECT ROUND(SUM(revenue),0) FROM cancellations c WHERE c.cancel_date = b.book_date), 0) AS cancel_rev
FROM new_bookings b
GROUP BY b.book_date
ORDER BY b.book_date
""")

rows = cur.fetchall()
conn.close()

total_rn = total_rev = total_res = total_crn = total_crev = 0
print(f"{'Date':<12} {'Res':>6} {'Bkd RN':>8} {'Bkd Rev':>12} {'Cxl RN':>8} {'Cxl Rev':>12} {'NET RN':>8} {'NET Rev':>12}")
print("-" * 80)
for r in rows:
    date, res, brn, brev, crn, crev = r
    brn = brn or 0; brev = brev or 0; crn = crn or 0; crev = crev or 0
    print(f"{str(date):<12} {res:>6} {brn:>8} {brev:>12,.0f} {crn:>8} {crev:>12,.0f} {brn-crn:>8} {brev-crev:>12,.0f}")
    total_res += res; total_rn += brn; total_rev += brev; total_crn += crn; total_crev += crev

print("-" * 80)
print(f"{'TOTAL':<12} {total_res:>6} {total_rn:>8} {total_rev:>12,.0f} {total_crn:>8} {total_crev:>12,.0f} {total_rn-total_crn:>8} {total_rev-total_crev:>12,.0f}")
print(f"\nBI comparison: 338 rn, EUR 142,878 rev, 84 reservations (gross booked)")
