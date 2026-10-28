"""
Demo twin sync tests — fake store, no database.
Run: py -3.13 test_demo_sync.py
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from datetime import date, timedelta
import inspect

from briefing import demo_sync
from briefing.demo_sync import anonymize, twin_row, sync_demo_briefings
from db import store

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond: passed += 1; print(f"  ok  {name}")
    else: failed += 1; print(f"  FAIL {name} {detail}")

SRC, TWIN = "11111111-aaaa-4aaa-8aaa-111111111111", "22222222-bbbb-4bbb-8bbb-222222222222"
PAIRS = [("City Hotel", "Aurora City Hotel"), ("City Hotel Thessaloniki", "Aurora City Hotel"),
         ("Thessaloniki", "the city")]

print("anonymize")
check("longest phrase wins", anonymize("City Hotel Thessaloniki", PAIRS) == "Aurora City Hotel",
      anonymize("City Hotel Thessaloniki", PAIRS))
check("short phrase still replaced", anonymize("at City Hotel today", PAIRS) == "at Aurora City Hotel today")
check("standalone city replaced", anonymize("demand in Thessaloniki", PAIRS) == "demand in the city")
check("no pairs = unchanged", anonymize("City Hotel", []) == "City Hotel")
check("demo name containing a real name is not rewritten twice",
      anonymize("City Hotel and City Hotel Thessaloniki", PAIRS) == "Aurora City Hotel and Aurora City Hotel",
      anonymize("City Hotel and City Hotel Thessaloniki", PAIRS))
check("regex characters in names are literal", anonymize("A+B (Ltd)", [("A+B (Ltd)", "Partner")]) == "Partner")

TODAY = date.today()
def src_row(d, name="City Hotel Thessaloniki"):
    return {"report_date": d, "data": {"hotel_name": name, "hotel_id": SRC, "yesterday": {"revenue": 12345.5},
                                        "note": "€1.234 — Καλημέρα"},
            "ai_insights": {"hero": f"{name} had a strong Tuesday in Thessaloniki."},
            "kpi_summary": None}
TW = {"id": TWIN, "source_id": SRC, "replace": PAIRS}

print("twin_row")
r = twin_row(src_row(TODAY - timedelta(days=1)), TW)
check("hotel_id is the twin", r["hotel_id"] == TWIN)
check("report_date is ISO text", r["report_date"] == (TODAY - timedelta(days=1)).isoformat(), r["report_date"])
check("name in data replaced", r["data"]["hotel_name"] == "Aurora City Hotel", r["data"]["hotel_name"])
check("real id inside the payload swapped", r["data"]["hotel_id"] == TWIN)
check("narration anonymized", r["ai_insights"]["hero"] == "Aurora City Hotel had a strong Tuesday in the city.",
      r["ai_insights"]["hero"])
check("numbers untouched", r["data"]["yesterday"]["revenue"] == 12345.5)
check("Greek and € survive", r["data"]["note"] == "€1.234 — Καλημέρα", r["data"]["note"])
check("kpi_summary carried (null ok)", "kpi_summary" in r and r["kpi_summary"] is None)
check("no real name left anywhere", "Thessaloniki" not in str(r) and "City Hotel Thessaloniki" not in str(r))

print("sync_demo_briefings")
class FakeStore:
    def __init__(self, twins, src_rows, have):
        self.twins, self.src_rows, self.have, self.written = twins, src_rows, have, []
    def demo_twins(self): return self.twins
    def get_briefings_since(self, hid, since, before, cols):
        self.window = (since, before)
        if hid == "boom": raise RuntimeError("PG down")
        return [x for x in self.src_rows.get(hid, []) if since <= str(x["report_date"]) < before]
    def get_briefing_dates(self, hid, limit): return self.have.get(hid, [])
    def upsert_briefing(self, row): self.written.append(row)

def run(fake):
    real = {k: getattr(store, k) for k in ("demo_twins", "get_briefings_since", "get_briefing_dates", "upsert_briefing")}
    try:
        for k in real: setattr(store, k, getattr(fake, k))
        sync_demo_briefings()
    finally:
        for k, v in real.items(): setattr(store, k, v)
    return fake

days = [TODAY - timedelta(days=d) for d in range(1, 21)]          # 20 days of source history
f = run(FakeStore([TW], {SRC: [src_row(d) for d in days]}, {}))
check("first sync backfills HISTORY_DAYS days only",
      len(f.written) == demo_sync.HISTORY_DAYS, len(f.written))   # since = today-14 → days 1..14 back
check("window ends tomorrow (today's row included)", f.window[1] == (TODAY + timedelta(days=1)).isoformat())
check("every written row belongs to the twin", all(w["hotel_id"] == TWIN for w in f.written))

have = [d.isoformat() for d in days[:14]]
f = run(FakeStore([TW], {SRC: [src_row(d) for d in days]}, {TWIN: have}))
check("next sync rewrites only the newest day", [w["report_date"] for w in f.written] == [days[0].isoformat()],
      [w["report_date"] for w in f.written])

f = run(FakeStore([TW], {SRC: [src_row(d) for d in days]}, {TWIN: have[2:]}))
check("missing days are filled + newest refreshed",
      sorted(w["report_date"] for w in f.written) == sorted([days[0].isoformat(), days[1].isoformat()]),
      [w["report_date"] for w in f.written])

bad = {"id": "33333333-cccc-4ccc-8ccc-333333333333", "source_id": "boom", "replace": []}
f = run(FakeStore([bad, TW], {SRC: [src_row(days[0])]}, {}))
check("one failing twin does not stop the others", len(f.written) == 1 and f.written[0]["hotel_id"] == TWIN)

f = run(FakeStore([], {SRC: [src_row(days[0])]}, {}))
check("no twins = no writes", f.written == [])

f = run(FakeStore([TW], {}, {}))
check("source without briefings = no writes", f.written == [])

print("store visibility (demo twins are inactive but visible)")
check("hotel_access_ids uses _VISIBLE", "_VISIBLE" in inspect.getsource(store.hotel_access_ids))
check("portfolio_groups uses _VISIBLE", "_VISIBLE" in inspect.getsource(store.portfolio_groups))
check("portfolio_group_hotels uses _VISIBLE", "_VISIBLE" in inspect.getsource(store.portfolio_group_hotels))
check("_VISIBLE keeps the active rule", store._VISIBLE.startswith("(h.active or "))
check("demo_twins is fail-open off PG", store.demo_twins() == [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
