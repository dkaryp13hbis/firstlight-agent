"""
Nightly off-platform backup of every Supabase table (2026-09-04).

Dumps each table as NDJSON.gz into  C:\\FirstLightBackups\\YYYY-MM-DD\\  and
prunes folders older than KEEP_DAYS. Runs on the operator's machine via Task
Scheduler (a different failure domain than Supabase/Railway — that is the
point). Credentials come from  C:\\FirstLightBackups\\backup.env  (never from
this repo).

Restore path (rehearsed 2026-09-04, see PHASE_C_RUNBOOK.md):
    py -3.13 scripts/backup_supabase.py --restore <folder> --table <name>
re-POSTs the rows with ignore-duplicates, so restoring over existing data is
idempotent (rows already present are left untouched).
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib import request, parse, error

ROOT = Path(r"C:\FirstLightBackups")
KEEP_DAYS = 14
TABLES = [
    "hotels", "hotel_users", "organizations", "briefings",
    "refresh_commands", "refresh_runs", "insight_feedback", "hotel_prefs",
    "push_subscriptions", "usage_events", "intraday_log", "watchlist",
]
PAGE = 50   # briefings rows are large (rendered_html); small pages keep responses sane

# Conflict targets for idempotent restore (PostgREST on_conflict)
CONFLICT = {
    "hotels": "id", "hotel_users": None, "organizations": "id",
    "briefings": "hotel_id,report_date", "refresh_commands": "id",
    "refresh_runs": "id", "insight_feedback": None, "hotel_prefs": "hotel_id",
    "push_subscriptions": None, "usage_events": "id",
    "intraday_log": "hotel_id,day,type", "watchlist": "id",
}


def creds() -> tuple[str, str]:
    env = ROOT / "backup.env"
    url = key = ""
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("SUPABASE_URL="):
                url = line.split("=", 1)[1].strip()
            if line.startswith("SUPABASE_SERVICE_KEY="):
                key = line.split("=", 1)[1].strip()
    url = url or os.getenv("SUPABASE_URL", "")
    key = key or os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        sys.exit("no credentials: create C:\\FirstLightBackups\\backup.env with "
                 "SUPABASE_URL=... and SUPABASE_SERVICE_KEY=...")
    return url.rstrip("/"), key


def fetch(url: str, key: str, table: str, offset: int) -> list:
    q = parse.urlencode({"select": "*", "limit": str(PAGE), "offset": str(offset)})
    req = request.Request(f"{url}/rest/v1/{table}?{q}",
                         headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def dump() -> None:
    url, key = creds()
    day_dir = ROOT / str(date.today())
    day_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for t in TABLES:
        rows, offset = [], 0
        try:
            while True:
                page = fetch(url, key, t, offset)
                rows.extend(page)
                if len(page) < PAGE:
                    break
                offset += PAGE
        except error.HTTPError as exc:
            print(f"  {t}: SKIPPED (HTTP {exc.code})")
            continue
        out = day_dir / f"{t}.ndjson.gz"
        with gzip.open(out, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        total += len(rows)
        print(f"  {t}: {len(rows)} rows -> {out.name} ({out.stat().st_size // 1024} KB)")
    # verify: every file reparses and counts match what we wrote
    for f in day_dir.glob("*.ndjson.gz"):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            n = sum(1 for line in fh if json.loads(line) is not None)
        print(f"  verify {f.name}: {n} rows parse OK")
    # prune old folders
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    for d in ROOT.iterdir():
        try:
            if d.is_dir() and date.fromisoformat(d.name) < cutoff:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
                print(f"  pruned {d.name}")
        except ValueError:
            continue
    (ROOT / "last_run.txt").write_text(f"{datetime.now().isoformat()} rows={total}\n", encoding="utf-8")
    print(f"backup complete: {total} rows, {date.today()}")


def restore(folder: str, table: str) -> None:
    url, key = creds()
    f = ROOT / folder / f"{table}.ndjson.gz"
    if not f.exists():
        sys.exit(f"no dump: {f}")
    with gzip.open(f, "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    conflict = CONFLICT.get(table)
    q = f"?on_conflict={parse.quote(conflict)}" if conflict else ""
    for i in range(0, len(rows), PAGE):
        body = json.dumps(rows[i:i + PAGE]).encode("utf-8")
        req = request.Request(f"{url}/rest/v1/{table}{q}", data=body, method="POST",
                             headers={"apikey": key, "Authorization": f"Bearer {key}",
                                      "Content-Type": "application/json",
                                      "Prefer": "resolution=ignore-duplicates,return=minimal"})
        with request.urlopen(req, timeout=120) as r:
            pass
    print(f"restored {len(rows)} rows into {table} (existing rows untouched)")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        folder = sys.argv[sys.argv.index("--restore") + 1]
        table = sys.argv[sys.argv.index("--table") + 1]
        restore(folder, table)
    else:
        dump()
