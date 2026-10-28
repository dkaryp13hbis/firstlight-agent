"""
One-time catch-up for the Supabase retirement (2026-10-28): copy every row
that exists ONLY in Supabase into Postgres. Insert-missing only — a row that
Postgres already has is never touched (Postgres is the authority). Safe to run
more than once. Runs INSIDE the web container (it has both stores' env):

    railway ssh -s web -- python scripts/supabase_catchup.py --dry-run
    railway ssh -s web -- python scripts/supabase_catchup.py
"""
import argparse
import os
import sys

import requests

sys.path.insert(0, ".")
from db import store  # noqa: E402

# table -> conflict target that identifies "the same row" in both stores
# hotels / organizations are NOT copied (Postgres is the registry; a stale
# Supabase hotel marked active would start running) — only reported.
REPORT_ONLY = ("hotels", "organizations")
TABLES = {
    "briefings": "hotel_id, report_date",
    "refresh_runs": "id",
    "refresh_commands": "id",
    "intraday_log": "hotel_id, day, type",
    "insight_feedback": "hotel_id, report_date, card_id, user_id",
    "hotel_prefs": "hotel_id",
    "push_subscriptions": "user_id, hotel_id",
    "watchlist": "id",
    "usage_events": "id",
    "subscriptions": "hotel_id",
}


def sb_rows(table: str, select: str = "*", filters: dict | None = None) -> list[dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    out, start, page = [], 0, 1000
    while True:
        r = requests.get(f"{url}/rest/v1/{table}", params={"select": select, **(filters or {})},
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Range-Unit": "items", "Range": f"{start}-{start + page - 1}"},
                         timeout=60)
        if r.status_code == 404 or (r.status_code == 400 and "does not exist" in r.text):
            return []
        r.raise_for_status()
        batch = r.json()
        out.extend(batch)
        if len(batch) < page:
            return out
        start += page


# big jsonb rows: a full `select *` times out on Supabase (HTTP 500) — read the
# keys first, then only the rows Postgres does not have, one at a time
KEYS_FIRST = {"briefings"}


def _missing_heavy_rows(table: str, key: str) -> list[dict]:
    cols = [c.strip() for c in key.split(",")]
    keys = sb_rows(table, select=",".join(cols))
    have = {tuple(str(v) for v in r) for r in store._exec(
        f"select {', '.join(c + '::text' for c in cols)} from {table}", fetch=True)}
    out = []
    for k in keys:
        kt = tuple(str(k[c]) for c in cols)
        if kt in have:
            continue
        out.extend(sb_rows(table, filters={c: f"eq.{k[c]}" for c in cols}))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if not store.read_from_pg():
        sys.exit("run inside the web container (STORAGE=pg + DATABASE_URL)")
    total = 0
    for table in REPORT_ONLY:
        try:
            pg_ids = {r[0] for r in store._exec(f"select id::text from {table}", fetch=True)}
            extra = [(r.get("id"), r.get("name"), r.get("active")) for r in sb_rows(table)
                     if r.get("id") not in pg_ids]
            print(f"{table:20} only in Supabase (NOT copied, review): {extra or 'none'}")
        except Exception as exc:
            print(f"{table:20} report failed: {exc}")
    for table, key in TABLES.items():
        try:
            if table in KEYS_FIRST:
                rows = _missing_heavy_rows(table, key)
            else:
                rows = sb_rows(table)
        except Exception as exc:
            print(f"{table:20} Supabase read failed: {exc}")
            continue
        cols_pg = store._columns(table)
        added = 0
        for r in rows:
            row = {k: v for k, v in r.items() if k in cols_pg}
            if not row:
                continue
            cols = list(row.keys())
            sql = (f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
                   f"on conflict ({key}) do nothing returning 1")
            if a.dry_run:
                where = " and ".join(f"{c.strip()} = %s" for c in key.split(","))
                vals = tuple(row.get(c.strip()) for c in key.split(","))
                if None in vals:
                    continue
                found = store._exec(f"select 1 from {table} where {where} limit 1", vals, fetch=True)
                added += 0 if found else 1
                continue
            try:
                got = store._exec(sql, tuple(store._jsonb(row[c]) for c in cols), fetch=True)
                added += 1 if got else 0
            except Exception as exc:
                print(f"{table:20} row skipped: {str(exc)[:120]}")
        total += added
        print(f"{table:20} supabase {len(rows):6}  {'missing' if a.dry_run else 'added  '} in PG {added:5}")
    print(("dry run — nothing written; " if a.dry_run else "") + f"{total} row(s) {'missing' if a.dry_run else 'added'}")


if __name__ == "__main__":
    main()
