"""Apply a SQL file to the Railway Postgres from INSIDE a container (the
database has no public address). Idempotent files only.

NOTE: the web container connects as fl_app (data-only, no DDL — grants.sql),
so this only works for DML fixes there. Migrations (CREATE / GRANT) run as
the builder role in the Postgres container instead — PowerShell:

    $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("docs\\sql\\pg\\<file>.sql"))
    railway ssh -s Postgres -- sh -c "echo $b64 | base64 -d | psql -v ON_ERROR_STOP=1 -U `$POSTGRES_USER -d `$POSTGRES_DB"

    railway ssh -s web -- python scripts/pg_apply.py docs/sql/pg/<dml-file>.sql
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
