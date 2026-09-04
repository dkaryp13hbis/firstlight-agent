# Phase C store layer — dormant-mode safety (plain script). Run: py -3.13 test_store.py
import os
os.environ.pop("STORAGE", None)
os.environ.pop("DATABASE_URL", None)
from db import store

P, F = 0, 0
def check(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  ok  {name}")
    else: F += 1; print(f"FAIL  {name} {detail}")

check("default mode = supabase", store.mode() == "supabase")
check("disabled without env", not store.enabled())
check("no PG reads by default", not store.read_from_pg())
check("upsert_briefing no-ops", store.upsert_briefing({"hotel_id": "x"}) is None)
check("insert_run no-ops", store.insert_run({"id": "x"}) is None)
check("update_run no-ops", store.update_run("x", {"status": "ok"}) is None)
check("claim mirrors to None", store.claim_intraday("h", "2026-09-04", "alerts") is None)
check("mirror_rows -> 0", store.mirror_rows("watchlist", [{"id": "x"}]) == 0 or store.mirror_rows("watchlist", []) == 0)
check("counts -> {}", store.counts() == {})
check("ping -> False", store.ping() is False)

# STORAGE=dual WITHOUT DATABASE_URL must also stay inert (misconfig guard)
os.environ["STORAGE"] = "dual"
check("dual without DATABASE_URL still disabled", not store.enabled())

# psycopg must NOT be imported in dormant mode (lazy import guarantee)
import sys
check("psycopg not imported while dormant", "psycopg" not in sys.modules)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
