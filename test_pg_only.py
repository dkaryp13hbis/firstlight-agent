"""
Postgres-only integration tests (Supabase retirement, 2026-10-28) — runs the
REAL SQL against a throwaway local Postgres. Skips unless FL_TEST_DATABASE_URL
is set. The database is wiped and rebuilt from docs/sql/pg (schema.sql +
migrations) on every run, so never point it at anything that matters.

    FL_TEST_DATABASE_URL=postgresql://postgres@localhost:5499/fltest?sslmode=disable \
        py -3.13 test_pg_only.py
"""
import io, os, sys, threading, uuid
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

URL = os.getenv("FL_TEST_DATABASE_URL")
if not URL:
    print("SKIP: FL_TEST_DATABASE_URL not set"); sys.exit(0)
os.environ.update({"STORAGE": "pg", "DATABASE_URL": URL, "AUTH": "own", "SUPABASE_ECHO": "0"})
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_HOTEL_ID", "VAPID_PRIVATE_KEY"):
    os.environ.pop(k, None)

import glob
from datetime import date, datetime, timedelta, timezone
import psycopg

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond: passed += 1; print(f"  ok  {name}")
    else: failed += 1; print(f"  FAIL {name} {detail}")

# ── fresh schema from the repo's own SQL ─────────────────────────────────────
with psycopg.connect(URL, autocommit=True) as c:
    c.execute("drop schema public cascade; create schema public")
    c.execute(open("docs/sql/pg/schema.sql", encoding="utf-8").read())
    for f in sorted(glob.glob("docs/sql/pg/migrations/*.up.sql")):
        c.execute(open(f, encoding="utf-8").read())

from db import store, auth
store._COLS_CACHE.clear()
check("store reads Postgres", store.read_from_pg() and store.ping())

YESTERDAY = str(date.today() - timedelta(days=1))
ORG = str(uuid.uuid4()); H1 = str(uuid.uuid4()); H2 = str(uuid.uuid4()); H3 = str(uuid.uuid4())
store._exec("insert into organizations (id, name, slug) values (%s, 'Test Co', 'test-co')", (ORG,))
for hid, name, active, token in ((H1, "Alpha Hotel", True, "flh_alpha"),
                                 (H2, "Beta Hotel", True, None),
                                 (H3, "Gamma Paused", False, "flh_gamma")):
    store._exec("insert into hotels (id, org_id, name, slug, total_rooms, active, api_token, pms_config) "
                "values (%s, %s, %s, %s, 50, %s, %s, '{}'::jsonb)",
                (hid, ORG, name, name.lower().replace(" ", "-"), active, token))

print("refresh queue")
ids = [store.command_insert(H1, "manual") for _ in range(3)]
check("command_insert returns ids", all(ids) and len(set(ids)) == 3, ids)
check("command_open_for finds the newest open one", (store.command_open_for(H1) or {}).get("id") == ids[-1])
check("command_open_for: none for another hotel = {}", store.command_open_for(H2) == {})
first = [c["id"] for c in store.commands_claim(2)]
check("claim takes 2 of the 3", len(first) == 2 and set(first) <= set(ids), first)
check("claimed rows are running", all(store.command_get(i)["status"] == "running" for i in first))
second = [c["id"] for c in store.commands_claim(2)]
check("next claim takes the one left", len(second) == 1 and set(first + second) == set(ids), second)
check("empty queue = []", store.commands_claim(2) == [])
ids = first + second            # claim order (queue order is by requested_at)
store.command_finish(ids[2])    # leave no open command behind for the API checks
check("command_finish marks done", store.command_finish(ids[0]) and store.command_get(ids[0])["status"] == "done")
check("completed_at is set", store.command_get(ids[0])["completed_at"] is not None)
check("command_finish failed status", store.command_finish(ids[1], "failed") and store.command_get(ids[1])["status"] == "failed")
check("finish of an unknown id = False", store.command_finish(str(uuid.uuid4())) is False)
check("command_get unknown = {}", store.command_get(str(uuid.uuid4())) == {})
# concurrency: 2 pollers, 8 commands, no command taken twice
more = [store.command_insert(H2, "data_only") for _ in range(8)]
got: list = []
def grab():
    for _ in range(6):
        got.extend(c["id"] for c in store.commands_claim(2))
ts = [threading.Thread(target=grab) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
check("two pollers never take the same command", sorted(got) == sorted(more) and len(got) == len(set(got)),
      f"{len(got)} taken, {len(set(got))} distinct")

print("refresh_runs")
rid = str(uuid.uuid4())
check("insert_run drops unknown fields instead of failing",
      store.insert_run({"id": rid, "hotel_id": H1, "run_type": "full", "status": "running",
                        "attempt": 1, "not_a_column": 5}))
check("update_run with unknown fields still writes the known ones",
      store.update_run(rid, {"status": "success", "fallbacks": 3, "rows_fetched": {"q1": 10},
                             "completed_at": datetime.now(timezone.utc).isoformat()}))
r = store.runs_since(H1, "2000-01-01", ["status", "rows_fetched"])
check("runs_since reads it back", r and r[0]["status"] == "success" and r[0]["rows_fetched"] == {"q1": 10}, r)

print("briefings")
check("upsert_briefing returns True",
      store.upsert_briefing({"hotel_id": H1, "report_date": YESTERDAY, "data": {"hotel_name": "Alpha"},
                             "ai_insights": {"insights": []}, "kpi_summary": {"x": 1}}))
check("upsert again (same day) = update, still True",
      store.upsert_briefing({"hotel_id": H1, "report_date": YESTERDAY, "data": {"hotel_name": "Alpha v2"},
                             "ai_insights": {}}))
check("get_briefing_on sees the update",
      (store.get_briefing_on(H1, YESTERDAY, ["data"]) or {}).get("data", {}).get("hotel_name") == "Alpha v2")
check("latest_report_dates", store.latest_report_dates() == {H1: YESTERDAY}, store.latest_report_dates())

print("push, intraday, prefs, feedback, subscriptions, hotels")
uid = str(uuid.uuid4())
check("push_any_for_user: none yet", store.push_any_for_user(uid) is False)
store.push_replace(uid, H1, {"endpoint": "https://push.example/1", "keys": {}})
subs = store.push_subs_for_hotel(H1)
check("push_subs_for_hotel", len(subs) == 1 and subs[0]["subscription"]["endpoint"].endswith("/1"), subs)
check("push_subs_for_hotel: other hotel empty", store.push_subs_for_hotel(H2) == [])
check("push_any_for_user: True", store.push_any_for_user(uid) is True)
check("claim_intraday first = True", store.claim_intraday(H1, YESTERDAY, "alerts") is True)
check("claim_intraday again = False", store.claim_intraday(H1, YESTERDAY, "alerts") is False)
store.feedback_upsert({"hotel_id": H1, "report_date": YESTERDAY, "card_id": "c1", "verdict": 1,
                       "reason": "useful", "card_content": None, "user_id": uid})
fb = store.feedback_for_hotel(H1, "2000-01-01")
check("feedback_for_hotel", fb and fb[0]["card_id"] == "c1" and fb[0]["reason"] == "useful", fb)
s1 = store.subscription_upsert({"hotel_id": H1, "plan": "trial", "status": "active", "price_eur": 99})
s2 = store.subscription_upsert({"hotel_id": H1, "plan": "monthly", "status": "active", "price_eur": 149})
check("subscription upsert inserts then updates", s1 and s2 and s2["plan"] == "monthly" and s2["price_eur"] == 149.0, s2)
check("subscriptions_list", [s["plan"] for s in store.subscriptions_list()] == ["monthly"])
allh = store.hotels_all(["id", "name", "active"])
check("hotels_all includes paused hotels", {h["name"] for h in allh} == {"Alpha Hotel", "Beta Hotel", "Gamma Paused"})
check("hotel_names", store.hotel_names().get(H3) == "Gamma Paused")
check("update_hotel_fields True / unknown id False",
      store.update_hotel_fields(H2, {"total_rooms": 60}) and not store.update_hotel_fields(str(uuid.uuid4()), {"total_rooms": 1}))

print("pipeline modules on Postgres only (no Supabase env)")
from briefing import cloud_push, intraday, run_log, audit
import railway_main
ok = cloud_push.push_to_cloud({"hotel_name": "Beta", "yesterday": {}, "mtd": {}, "pace": [], "pickup": {}},
                              {"insights": [], "_meta": {"x": 1}}, hotel_id=H2, notify=False)
check("push_to_cloud publishes to Postgres with no Supabase env", ok is True)
row = store.get_briefing_on(H2, YESTERDAY, ["ai_insights", "kpi_summary"])
check("published row has no _meta and has kpi_summary", row and "_meta" not in row["ai_insights"] and row["kpi_summary"], row)
check("_briefing_exists_today reads Postgres", railway_main._briefing_exists_today(H2) is True
      and railway_main._briefing_exists_today(H3) is False)
lg = run_log.RunLogger(H2, "full")
lg.start()
check("RunLogger opens a Postgres row", lg.run_id and store.runs_since(H2, "2000-01-01", ["status"])[0]["status"] == "running")
lg.record(rows_fetched={"q1": 3}, made_up_field=1)
lg.finish("success")
check("RunLogger closes it", store.runs_since(H2, "2000-01-01", ["status", "rows_fetched"])[0] == {"status": "success", "rows_fetched": {"q1": 3}})
check("intraday _claim decides on Postgres", intraday._claim(H2, "momentum") is True and intraday._claim(H2, "momentum") is False)
check("push send reads Postgres subs (no VAPID = skipped cleanly)", cloud_push._subscriptions(H1)[0]["subscription"]["endpoint"].endswith("/1"))
audit._CACHE["at"] = 0
check("stale_hotels from Postgres", audit.stale_hotels() == [], audit.stale_hotels())
store._exec("delete from briefings where hotel_id = %s", (H2,)); audit._CACHE["at"] = 0
check("stale_hotels flags a hotel without a fresh briefing", audit.stale_hotels() == ["Beta Hotel"], audit.stale_hotels())
store._exec("insert into refresh_commands (id, hotel_id, type) values (%s, %s, 'manual')", (cid := str(uuid.uuid4()), H3))
store.commands_claim(5)
railway_main.run_all_hotels(hotel_id_filter=H3, cmd_id=cid, force=True, manual=True)
check("a refresh for a paused hotel is closed as failed", store.command_get(cid)["status"] == "failed")

print("API on Postgres only")
try:
    from fastapi.testclient import TestClient
except Exception as exc:
    print(f"  (skipped: {exc})"); TestClient = None
if TestClient:
    import api
    api._TOKENS["at"] = 0
    cl = TestClient(api.app)
    u = auth.create_user("gm@alpha.test", "GM", "Passw0rd123", "en", False, must_change=False)
    auth.grant(u["id"], "hotel", H1, "viewer")
    adm = auth.create_user("ops@hbis.test", "Ops", "Passw0rd123", "en", True, must_change=False)
    tok = cl.post("/auth/login", json={"email": "gm@alpha.test", "password": "Passw0rd123"}).json()["token"]
    atok = cl.post("/auth/login", json={"email": "ops@hbis.test", "password": "Passw0rd123"}).json()["token"]
    H = {"Authorization": f"Bearer {tok}"}; A = {"Authorization": f"Bearer {atok}"}
    r1 = cl.post(f"/app/hotels/{H1}/refresh", headers=H)
    check("POST /app/hotels/{id}/refresh queues", r1.status_code == 202 and r1.json()["joined"] is False, r1.text)
    r2 = cl.post(f"/app/hotels/{H1}/refresh", headers=H)
    check("a second tap joins the open refresh", r2.json()["id"] == r1.json()["id"] and r2.json()["joined"] is True, r2.text)
    check("refresh for a hotel the user can't see = 403", cl.post(f"/app/hotels/{H2}/refresh", headers=H).status_code == 403)
    st = cl.get(f"/app/refresh/{r1.json()['id']}", headers=H)
    check("GET /app/refresh/{id} = pending", st.status_code == 200 and st.json()["status"] == "pending", st.text)
    store.command_finish(r1.json()["id"])
    check("… then done", cl.get(f"/app/refresh/{r1.json()['id']}", headers=H).json()["status"] == "done")
    check("unknown refresh = 404", cl.get(f"/app/refresh/{uuid.uuid4()}", headers=H).status_code == 404)
    check("/push/any false for the GM", cl.get("/push/any", headers=H).json() == {"any": False})
    store.push_replace(u["id"], H1, {"endpoint": "https://push.example/gm", "keys": {}})
    check("/push/any true after subscribing", cl.get("/push/any", headers=H).json() == {"any": True})
    T = {"Authorization": "Bearer flh_alpha"}
    lb = cl.get(f"/briefing/latest?hotel_id={H1}", headers=T)
    check("hotel-token /briefing/latest from Postgres", lb.status_code == 200 and lb.json()["report_date"] == YESTERDAY, lb.text[:200])
    check("hotel-token /runs from Postgres", cl.get(f"/runs?hotel_id={H1}", headers=T).json()["runs"][0]["status"] == "success")
    check("paused hotel's token is refused", cl.get(f"/briefing/latest?hotel_id={H3}",
          headers={"Authorization": "Bearer flh_gamma"}).status_code == 403)
    ah = cl.get("/admin/hotels", headers=A)
    by = {h["name"]: h for h in ah.json().get("hotels", [])}
    check("/admin/hotels lists paused hotels + last briefing", ah.status_code == 200
          and "Gamma Paused" in by and by["Alpha Hotel"]["last_briefing"] == YESTERDAY, ah.text[:200])
    q = cl.post(f"/admin/hotels/{H2}/refresh", headers=A)
    check("portal Refresh now queues in Postgres", q.status_code == 202 and store.command_get(q.json()["id"])["status"] == "pending")
    check("portal Pause writes Postgres", cl.post(f"/admin/hotels/{H2}/active", headers=A, json={"active": False, "reason": "test"}).status_code == 200
          and not {h["id"]: h for h in store.hotels_all(["id", "active"])}[H2]["active"])
    sp = cl.put(f"/admin/subscription/{H1}", headers=A, json={"plan": "annual", "status": "active", "price_eur": 990})
    check("portal subscription write goes to Postgres", sp.status_code == 200 and store.subscriptions_list()[0]["plan"] == "annual", sp.text)
    audit._CACHE["at"] = 0
    hl = cl.get("/health").json()
    check("/health stale list from Postgres", hl["stale_hotels"] == [], hl)

print("the Supabase echo can never block (production keeps it on during the switch)")
import time as _t
os.environ.update({"SUPABASE_URL": "http://127.0.0.1:9", "SUPABASE_SERVICE_KEY": "x", "SUPABASE_ECHO": "1"})
from db import sb_echo
check("echo is on with env set", sb_echo.enabled())
t0 = _t.time()
ok = cloud_push.push_to_cloud({"hotel_name": "Alpha", "yesterday": {}, "mtd": {}, "pace": [], "pickup": {}},
                              {"insights": []}, hotel_id=H1, notify=False)
check("publish succeeds with an unreachable Supabase", ok is True)
check("… and fast (echo failure is not waited on for long)", _t.time() - t0 < 10, f"{_t.time() - t0:.1f}s")
lg = run_log.RunLogger(H1, "data_only"); lg.start(); lg.finish("success")
check("RunLogger still logs to Postgres", store.runs_since(H1, "2000-01-01", ["status", "run_type"])[0] == {"status": "success", "run_type": "data_only"})
real = store.upsert_briefing
store.upsert_briefing = lambda payload: False
try:
    ok = cloud_push.push_to_cloud({"hotel_name": "Alpha"}, {"insights": []}, hotel_id=H1, notify=False)
finally:
    store.upsert_briefing = real
check("Postgres refusal → Supabase fallback tried → reported as not published", ok is False)
os.environ["SUPABASE_ECHO"] = "0"
check("SUPABASE_ECHO=0 switches every echo off", not sb_echo.enabled())

store._pool().close()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
