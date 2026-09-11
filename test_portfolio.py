"""
Portfolio builder tests — synthetic briefings, no database.
Run: py -3.13 test_portfolio.py
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from datetime import date, timedelta
from briefing.portfolio import build_portfolio, build_hotel

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond: passed += 1; print(f"  ok  {name}")
    else: failed += 1; print(f"  FAIL {name} {detail}")

REF = date(2026, 9, 10)
TODAY = REF + timedelta(days=1)

def briefing(rdate=REF, rooms_sold=50, rev=5000.0):
    pace = [{"month_num": m, "rn": 1000 + m, "rev": 100000.0 + m, "rn_stly": 900, "rev_stly": 90000.0,
             "rn_final_ly": 950 + m, "rev_final": 95000.0 + m} for m in range(1, 13)]
    return {"report_date": rdate, "data": {
        "yesterday": {"roomNights": rooms_sold, "revenue": rev, "roomNightsLY": 40, "revenueLY": 4000.0, "revenueNet": rev / 1.13},
        "mtd": {"roomNights": 500, "revenue": 50000.0, "roomNightsLY": 450, "revenueLY": 45000.0},
        "pace": pace,
        "pickup": {"last1d": {"roomNights": 10, "revenue": 1000}, "last3d": {"roomNights": 30, "revenue": 3000},
                   "last7d": {"roomNights": 70, "revenue": 7000}, "cancellations1d": 1, "cancellationRevenue": 100,
                   "cancellations3d": 3, "cancellationRevenue3d": 300, "cancellations7d": 7, "cancellationRevenue7d": 700},
        "pickup_daily": [{"ref_date": (TODAY - timedelta(days=d)).isoformat(), "net_rn": 5, "net_rev": 500} for d in range(20)],
        "cancel_daily": [{"ref_date": (TODAY - timedelta(days=d)).isoformat(), "cancel_rn": 1, "cancel_rev": 100} for d in range(20)],
        "otb_by_date": [{"stay_date": (TODAY + timedelta(days=d)).isoformat(), "rn_ty": 80, "rn_stly": 60} for d in range(90)],
    }}

H1 = {"id": "h1", "name": "City Hotel Thessaloniki", "total_rooms": 100, "season_settings": None, "pms_config": {}}
H2 = {"id": "h2", "name": "The Excelsior Hotel", "total_rooms": 40,
      "season_settings": {"2026": {"open": "2026-04-15", "close": "2026-11-01"}}, "pms_config": {"place": "Halkidiki"}}
H3 = {"id": "h3", "name": "Old Hotel", "total_rooms": 20, "season_settings": None, "pms_config": {}}

P = build_portfolio("G", [H1, H2, H3], {"h1": briefing(), "h2": briefing(), "h3": briefing(rdate=REF - timedelta(days=3))})
by = {h["id"]: h for h in P["hotels"]}

print("group")
check("reportDate = newest report", P["reportDate"] == "2026-09-10")
check("label", P["reportLabel"] == "Thu, Sep 10", P["reportLabel"])
check("cur/elapsed", (P["cur"], P["elapsed"]) == (9, 10))
check("netFactor ≈ 1/1.13 from fresh hotels", abs(P["netFactor"] - 1 / 1.13) < 1e-6, P["netFactor"])
check("stale hotel flagged with its own date, sorted last", by["h3"]["stale"] == "2026-09-07" and P["hotels"][-1]["id"] == "h3")

print("blocks")
h = by["h1"]
check("yd block", h["yd"] == {"rnTY": 50, "revTY": 5000.0, "rnLY": 40, "revLY": 4000.0, "avail": 100})
check("mtd avail = rooms × elapsed", h["mtd"]["avail"] == 1000)
check("ytd = past months + mtd (rn)", h["ytd"]["rnTY"] == sum(1000 + m for m in range(1, 9)) + 500, h["ytd"]["rnTY"])
check("ytd LY = final LY past months + mtd LY", h["ytd"]["rnLY"] == sum(950 + m for m in range(1, 9)) + 450)
check("ytd avail = 243 days × rooms", h["ytd"]["avail"] == 100 * (243 + 10), h["ytd"]["avail"])
check("short name", h["short"] == "City Hotel" and by["h2"]["short"] == "Excelsior", (h["short"], by["h2"]["short"]))
check("place from pms_config", by["h2"]["place"] == "Halkidiki")

print("season")
s = by["h2"]
check("open Apr–Nov only", [m["open"] for m in s["months"]] == [False, False, False, True, True, True, True, True, True, True, True, False])
check("closed month avail 0", s["months"][0]["avail"] == 0 and s["months"][8]["avail"] == 40 * 30)
check("closedNow false in Sep", s["closedNow"] is False)
check("ytd avail = whole open months + elapsed (Apr–Aug full + 10 Sep days)", s["ytd"]["avail"] == 40 * (30 + 31 + 30 + 31 + 31 + 10), s["ytd"]["avail"])

print("pickup / next")
check("windows 1/3/7 from card", h["pickup"][7] == {"rn": 70, "rev": 7000.0, "cancel": 7, "cancelRev": 700.0})
check("14d = net + cancels over 14 days", h["pickup"][14] == {"rn": 14 * 5 + 14, "rev": 14 * 500 + 1400, "cancel": 14, "cancelRev": 1400})
check("next has 30 days starting today", len(h["next"]) == 30 and (h["next"][0]["dom"], h["next"][0]["m"]) == (11, 9))
check("next occupancy = rn / rooms", abs(h["next"][0]["ty"] - 0.8) < 1e-9 and abs(h["next"][0]["st"] - 0.6) < 1e-9)
check("dow is JS convention (Fri 11 Sep 2026 = 5)", h["next"][0]["dow"] == 5, h["next"][0]["dow"])
check("hotel without briefing → stale, zero blocks", build_hotel(H3, None, REF)["stale"] == REF.isoformat())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
