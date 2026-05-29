# Diagnostic: What are the Occupancy=0 rows?

import pyodbc
from datetime import date, timedelta

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=192.168.100.7;DATABASE=bidata;UID=sa;PWD=@Hitprotel#;",
    timeout=15
)
cur = conn.cursor()
yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

# Breakdown of Occupancy=0 rows by category type and whether fake
print(f"=== Occupancy=0 rows by katnr for {yesterday} ===")
cur.execute("""
    SELECT
        h.katnr,
        k.kat          AS cat_code,
        k.bez          AS cat_name,
        k.zimmer       AS zimmer_flag,
        COUNT(*)       AS rows,
        SUM(h.logis)   AS revenue
    FROM bidata.proteluser.Hitia h
    JOIN protel.proteluser.kat k ON k.katnr = h.katnr
    WHERE h.mpehotel = 1
      AND h.reschar < 2
      AND CAST(h.[date] AS DATE) = ?
      AND h.Occupancy = 0
    GROUP BY h.katnr, k.kat, k.bez, k.zimmer
    ORDER BY COUNT(*) DESC
""", yesterday)
rows = cur.fetchall()
for r in rows:
    print(f"  katnr={r[0]}  code={r[1]!r:8} name={r[2]!r:30} zimmer={r[3]}  rows={r[4]}  rev={r[5]}")

# Also show Occupancy=1 rows for comparison
print(f"\n=== Occupancy=1 rows by katnr for {yesterday} ===")
cur.execute("""
    SELECT
        h.katnr,
        k.kat          AS cat_code,
        k.bez          AS cat_name,
        k.zimmer       AS zimmer_flag,
        COUNT(*)       AS rows,
        SUM(h.logis)   AS revenue
    FROM bidata.proteluser.Hitia h
    JOIN protel.proteluser.kat k ON k.katnr = h.katnr
    WHERE h.mpehotel = 1
      AND h.reschar < 2
      AND CAST(h.[date] AS DATE) = ?
      AND h.Occupancy = 1
    GROUP BY h.katnr, k.kat, k.bez, k.zimmer
    ORDER BY COUNT(*) DESC
""", yesterday)
rows = cur.fetchall()
for r in rows:
    print(f"  katnr={r[0]}  code={r[1]!r:8} name={r[2]!r:30} zimmer={r[3]}  rows={r[4]}  rev={r[5]}")

conn.close()
print("\nDone.")
