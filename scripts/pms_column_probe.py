"""
READ-ONLY column inventory for the source-analysis fields (2026-10-30):
market SEGMENT, RATE plan / rate code, ROOM CATEGORY names, GROUP / block id.
Lists column NAMES (and types) only — reads no booking data — so the next
queries can use real column names instead of guesses.

Opera (Tor group, Oracle):
    set ORA_PW=...   &  py -3.13 scripts/pms_column_probe.py opera [host:port]
    (default host 192.168.0.156:1521 = VPN; or a local `cloudflared access tcp` port)
Protel (SQL Server, uses the SQL_* settings in .env):
    py -3.13 scripts/pms_column_probe.py protel

Prints every column of the tables we read, flagging the likely candidates.
Paste the output back into the session.
"""
import os
import re
import sys

CANDIDATE = re.compile(r"(?i)market|segment|seg_|rate|price|preis|room_?cat|room_?type|kat|categ|"
                       r"group|block|allot|contract|company|travel|agent|source|channel|origin")


def show(table: str, cols: list[tuple]) -> None:
    print(f"\n=== {table} — {len(cols)} columns")
    for name, typ in cols:
        flag = "  <-- candidate" if CANDIDATE.search(name) else ""
        print(f"  {name:40s} {typ}{flag}")


def opera(host: str) -> None:
    import oracledb
    conn = oracledb.connect(user="opera_ro", password=os.environ["ORA_PW"], dsn=f"{host}/opera")
    cur = conn.cursor()
    for owner, table in (("OPERA", "EUROTEL_TARGIT_WBLKNM"), ("OPERA", "RESORT$_ROOM_CATEGORY")):
        cur.execute("select column_name, data_type from all_tab_columns "
                    "where owner = :o and table_name = :t order by column_id", o=owner, t=table)
        show(f"{owner}.{table}", cur.fetchall())


def protel() -> None:
    import pyodbc
    from pathlib import Path
    env = {}
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    parts = [f"DRIVER={{ODBC Driver 17 for SQL Server}}", f"SERVER={env['SQL_SERVER']}"]
    if env.get("SQL_TRUSTED", "").lower() in ("1", "true", "yes"):
        parts.append("Trusted_Connection=yes")
    else:
        parts += [f"UID={env['SQL_USER']}", f"PWD={env['SQL_PASSWORD']}"]
    conn = pyodbc.connect(";".join(parts), timeout=15)
    cur = conn.cursor()
    for db, table in (("bidata", "Hitia"), ("protel", "kat")):
        cur.execute(f"select COLUMN_NAME, DATA_TYPE from {db}.INFORMATION_SCHEMA.COLUMNS "
                    f"where TABLE_SCHEMA = 'proteluser' and TABLE_NAME = ? order by ORDINAL_POSITION", table)
        show(f"{db}.proteluser.{table}", [tuple(r) for r in cur.fetchall()])


if __name__ == "__main__":
    which = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if which == "opera":
        opera(sys.argv[2] if len(sys.argv) > 2 else "192.168.0.156:1521")
    elif which == "protel":
        protel()
    else:
        print(__doc__)
