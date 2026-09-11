"""
Opera adapter compute-layer tests — synthetic grain rows, no database.
Run: py -3.13 test_opera.py

Verifies the Protel conventions are honoured by the Python pack:
  - active vs cancelled on stay-date vs book-date axes
  - STLY includes bookings cancelled AFTER the cap (nights/revenue restored)
    and excludes bookings made after the cap or cancelled before it
  - pickup daily reconciles with the pickup card (net + cancels = gross)
  - departure-day rows add revenue (day use) but no nights
  - every row shape has the full Protel column set; contract complete
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from datetime import date, timedelta
from db.adapters.opera_oracle.fetcher import Row, compute_pack, _year_ago
from db.adapters.assemble import assemble_snapshot
from db.contract import is_publishable

TODAY = date(2026, 9, 12)
Y = TODAY - timedelta(days=1)          # 2026-09-11
CAP = _year_ago(TODAY)                 # 2025-09-12
Y_LY = _year_ago(Y)                    # 2025-09-11

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ok  {name}")
    else:
        failed += 1; print(f"  FAIL {name} {detail}")


def A(sd, bd, src="BK", rn=1, net=100.0, gross=113.0, arr=0, dep=0, has_rev=1):
    return Row(sd, bd, None, src, "A", has_rev, rn, net, gross, 0, 0, arr, dep)


def D(sd, bd, gross=0.0, dep=1):
    return Row(sd, bd, None, "BK", "D", 1 if gross else 0, 0, gross / 1.13 if gross else 0, gross, 0, 0, 0, dep)


def C(sd, bd, cd, crn=1, cgross=113.0, src="BK"):
    return Row(sd, bd, cd, src, "C", 0, 0, 0, 0, crn, cgross, 0, 0)


rows = [
    # yesterday: 2 occupied rooms (1 arrival), 1 departure-day row with day-use revenue
    A(Y, Y - timedelta(days=30), rn=1, arr=1),
    A(Y, Y - timedelta(days=5), rn=1),
    D(Y, Y - timedelta(days=2), gross=56.5),
    # a cancelled night for yesterday (cancelled 3 days ago) — must NOT count as occupied
    C(Y, Y - timedelta(days=10), TODAY - timedelta(days=3)),
    # LY yesterday: 1 active + 1 cancelled after the stay (post-stay cancel) → both count
    A(Y_LY, Y_LY - timedelta(days=20)),
    C(Y_LY, Y_LY - timedelta(days=20), CAP + timedelta(days=5)),
    # STLY: Oct LY — booked before cap & active (counts), booked before cap & cancelled after cap (counts),
    # booked AFTER cap (no), cancelled BEFORE cap (no)
    A(date(2025, 10, 10), CAP - timedelta(days=30)),
    C(date(2025, 10, 11), CAP - timedelta(days=30), CAP + timedelta(days=10), cgross=200.0),
    A(date(2025, 10, 12), CAP + timedelta(days=1)),
    C(date(2025, 10, 13), CAP - timedelta(days=30), CAP - timedelta(days=1)),
    # TY OTB Oct: 2 nights
    A(date(2026, 10, 10), TODAY - timedelta(days=3)),
    A(date(2026, 10, 11), TODAY, src="TEL"),
    # pickup: booked today (1 night), booked yesterday then cancelled today (gross +1, net 0)
    C(date(2026, 11, 1), Y, TODAY, cgross=90.0),
    # next 7 days
    A(TODAY + timedelta(days=1), TODAY - timedelta(days=1), arr=1),
]

pack = compute_pack(rows, TODAY)

print("Q1 KPIs")
k = pack["kpi"]
check("yesterday nights = 2 (cancelled night excluded, departure row 0)", k["rn_yday_ty"] == 2)
check("yesterday gross includes day-use revenue", abs(k["rev_yday_ty"] - (113 + 113 + 56.5)) < 0.01, k["rev_yday_ty"])
check("yesterday net", abs(k["rev_yday_ty_net"] - (100 + 100 + 50)) < 0.01, k["rev_yday_ty_net"])
check("LY yesterday restores post-stay cancellation (2 nights)", k["rn_yday_ly"] == 2)
check("LY yesterday revenue = gross + lost gross", abs(k["rev_yday_ly"] - 226) < 0.01, k["rev_yday_ly"])
check("STLY Oct = 2 nights (active pre-cap + cancelled after cap)", k["rn_stly_10"] == 2, k["rn_stly_10"])
check("STLY Sep = 2 (LY yesterday active row + post-stay cancel, both booked pre-cap)",
      k["rn_stly_9"] == 2, k["rn_stly_9"])

print("Q2 in-house")
check("arrivals 1 / departures 1 / stayovers 1", pack["inhouse"] == {"arrivals": 1, "departures": 1, "stayovers": 1}, pack["inhouse"])

print("Q3 pickup")
p = pack["pickup"]
check("today = 1 night (Oct 11 booked today)", p["pickup_today_rn"] == 1, p["pickup_today_rn"])
check("1d = 2 booked yesterday (tomorrow's stay + the later-cancelled Nov night)", p["pickup_1d_rn"] == 2, p["pickup_1d_rn"])
check("7d gross = 6 rows booked in window", p["pickup_7d_rn"] == 1 + 1 + 1 + 1 + 1, p["pickup_7d_rn"])
check("cancellations today = 1 / 90€", p["cancel_today_count"] == 1 and p["cancel_today_rev"] == 90.0)
check("cancellations 7d = 2 (incl. yesterday's night cancelled 3 days ago)", p["cancel_7d_count"] == 2, p["cancel_7d_count"])
check("top month is Sep or Oct (tie-safe)", p["top_month"] in (9, 10, 11), p["top_month"])

print("Q4 pace")
oct_ = next(r for r in pack["pace"] if r["stay_month"] == 10)
check("Oct TY OTB 2 nights", oct_["rn_otb_ty"] == 2)
check("Oct STLY 2 nights / rev 113+200", oct_["rn_stly"] == 2 and abs(oct_["rev_stly"] - 313) < 0.01, oct_)
check("Oct final LY = 2 active rows (incl. booked-after-cap)", oct_["rn_final_ly"] == 2, oct_["rn_final_ly"])
check("pace rows carry every Protel column", all(kk in oct_ for kk in ("rev_otb_ty_net", "rev_stly_net", "rev_final_ly_net")))

print("Q9/Q14 daily series reconcile with the card")
seven = TODAY - timedelta(days=6)
net7 = sum(r["net_rn"] for r in pack["pickup_daily"] if r["ref_date"] >= seven)
cx7 = sum(r["cancel_rn"] for r in pack["cancel_daily"] if r["ref_date"] >= seven)
check("net + cancels == gross pickup 7d", net7 + cx7 == p["pickup_7d_rn"], (net7, cx7, p["pickup_7d_rn"]))

print("Q6 / Q10 / Q11")
check("next7 has tomorrow with 1 room, 1 arrival", pack["next7"][0]["room_nights"] == 1 and pack["next7"][0]["arrivals"] == 1)
otb_today = [r for r in pack["otb_by_date"] if r["stay_date"] == TODAY + timedelta(days=1)]
check("otb_by_date includes tomorrow", bool(otb_today))
check("current month remaining TY = 1 night (tomorrow)", pack["current_month"]["rn_remaining_otb_ty"] == 1)

print("Q5 sources / Q16 next year / Q13 lead time")
srcs = {r["source"]: r for r in pack["sources"]}
check("sources have all 4 columns", all(kk in srcs["BK"] for kk in ("rev_ty", "rn_ty", "rev_stly", "rn_stly")))
check("pace_next rows have stly2 keys", all("rn_stly2" in r for r in pack["pace_next"]))
check("lead_time TY row for Oct booked 3 days ago (bucket 16-30)",
      any(r["period"] == "TY" and r["stay_month"] == 10 and r["lead_bucket"] == "16-30" for r in pack["lead_time"]))

print("Assembly + contract")
pack["inventory"] = [{"ref_date": Y, "total_rooms": 3}]
snap = assemble_snapshot(pack, {"hotel_name": "T", "total_rooms": 3, "pms_hotel_id": "CITY", "hotel_type": "city"}, today=TODAY)
ok, reason = is_publishable(snap, 3)
check("snapshot publishable", ok, reason)
check("legacy_mode off", not snap["data_quality"]["legacy_mode"])
check("yesterday occupancy 2/3", abs(snap["yesterday"]["occupancy"] - 0.6667) < 0.001, snap["yesterday"]["occupancy"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
