"""Apply a SQL file to the Railway Postgres from INSIDE the web container
(the database has no public address). Idempotent files only.

    railway ssh -s web -- python scripts/pg_apply.py docs/sql/pg/<file>.sql
"""
import os, sys
import psycopg

path = sys.argv[1]
sql = open(path, encoding="utf-8").read()
url = os.environ["DATABASE_URL"]
if "sslmode=" not in url:
    url += ("&" if "?" in url else "?") + "sslmode=require"
with psycopg.connect(url, autocommit=True) as conn:
    conn.execute(sql)
print(f"applied {path}")
