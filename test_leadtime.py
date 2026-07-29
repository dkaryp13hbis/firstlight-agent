"""
Signal 3 (booking lead time) — compute-layer tests. No DB, no Claude.

Run:  python test_leadtime.py
"""

import calendar
from datetime import date, timedelta

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

from briefing.analyst import _compute_signals
from db.contract import build_data_quality

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name} {detail}")


today = date.today()
# Pick a stay month safely in the future (next month)
nm_year = today.year if today.month < 12 else today.year + 1
nm = today.month + 1 if today.month < 12 else 1
nm_name = calendar.month_abbr[nm]

# Month after next — used for the below-gate case
n2 = nm + 1 if nm < 12 else 1
n2_year = nm_year if nm < 12 else nm_year + 1


def lt_row(period, sm, sy, source, bucket, rn, avg_lead, rev):
    return {"period": period, "stay_month": sm, "stay_year": sy,
            "source": source, "lead_bucket": bucket,
            "rn": rn, "rev": rev, "lead_x_rn": rn * avg_lead}


lead_time = [
    # ── Case A: next month, big shrink (wavg 15.2 vs 31.7 ≈ −52%) ──
    lt_row("TY", nm, nm_year, "OTA", "8-15", 30, 12, 4500),
    lt_row("TY", nm, nm_year, "Direct", "16-30", 20, 20, 3000),
    lt_row("LY", nm, nm_year - 1, "OTA", "16-30", 25, 25, 3800),
    lt_row("LY", nm, nm_year - 1, "Direct", "31-60", 20, 40, 3200),
    # ── Case B: month after, tiny shift (~5%) → must NOT gate through ──
    lt_row("TY", n2, n2_year, "OTA", "16-30", 40, 21, 6000),
    lt_row("LY", n2, n2_year - 1, "OTA", "16-30", 40, 20, 5800),
    # ── Case C: low volume (rn < 15) → must be skipped ──
    lt_row("TY", 12, nm_year, "Direct", "31-60", 10, 50, 9000),
    lt_row("LY", 12, nm_year - 1, "Direct", "31-60", 9, 30, 8000),
]

data = {
    "hotel_name": "Test Hotel",
    "report_date": "test",
    "yesterday": {"revenue": 10000, "occupancy": 0.8},
    "mtd": {"revenue": 200000, "month_name": today.strftime("%B")},
    "pace": [],
    "current_month_remaining": {},
    "lead_time": lead_time,
    "hotel_type": "resort",
}

res = _compute_signals(data, hotel_id=None)
all_cands = res["ranked"] + res["watchlist"]
lt = [c for c in all_cands if c["signal"] == "lead_time"]

print(f"— Signal 3 compute ({len(lt)} lead_time candidate(s)) —")
check("exactly one candidate survives the gates", len(lt) == 1,
      f"got {[c['insight']['id'] for c in lt]}")

if lt:
    c = lt[0]
    ins = c["insight"]
    facts = ins["facts"]
    check("candidate is for next month",
          ins["id"] == f"leadtime_{nm_name.lower()}_{nm_year}", ins["id"])
    check("shrinking window → MONITOR tag", c["tag"] == "MONITOR", c["tag"])
    check("avg lead TY = 15 days", facts["avg_lead"]["value"] == "15 days",
          facts["avg_lead"]["value"])
    check("avg lead LY = 32 days", facts["avg_lead_ly"]["value"] == "32 days",
          facts["avg_lead_ly"]["value"])
    check("shift fact negative", facts["lead_shift"]["value"].startswith("−")
          or facts["lead_shift"]["value"].startswith("-"),
          facts["lead_shift"]["value"])
    check("stake = €7,500", facts["value_at_stake"] == "€7,500",
          facts["value_at_stake"])
    check("close-in share TY = 60%", facts["close_in_share"]["value"] == "60%",
          facts["close_in_share"]["value"])
    check("close-in share LY = 0%", facts["close_in_share_ly"]["value"] == "0%",
          facts["close_in_share_ly"]["value"])
    check("top source drill-down present", "top_source_shift" in facts,
          str(facts.keys()))
    if "top_source_shift" in facts:
        # Direct shifted −20d (20 vs 40), OTA −13d (12 vs 25) → Direct wins
        check("top source is Direct (largest shift)",
              facts["top_source_shift"]["value"] == "Direct: 20d vs 40d",
              facts["top_source_shift"]["value"])
    check("facts are period-scoped", all(
        isinstance(v, dict) and "period" in v
        for k, v in facts.items()
        if k not in ("month_label", "value_at_stake", "value_at_stake_calc")))
    fb = c["fallback_card"]
    check("fallback has 2 evidence rows", len(fb["evidence"]) == 2)
    check("fallback headline mentions month", nm_name in fb["headline"], fb["headline"])
    # Word-limit contract: fallback templates must respect every cap
    from briefing.analyst import _WORD_CAPS
    for field, cap in _WORD_CAPS.items():
        n = len(str(fb.get(field, "")).split())
        check(f"fallback {field} within cap ({n}<={cap})", n <= cap,
              str(fb.get(field, "")))
    check("directive is hold_rates",
          ins["action_directives"]["type"] == "hold_rates",
          ins["action_directives"]["type"])

# City profile: close buckets tighten to ≤7d → share drops to 0/0 for case A
data_city = dict(data, hotel_type="city")
res_city = _compute_signals(data_city, hotel_id=None)
lt_city = [c for c in res_city["ranked"] + res_city["watchlist"]
           if c["signal"] == "lead_time"]
print("— City profile —")
check("city profile still produces the candidate", len(lt_city) == 1)
if lt_city:
    f = lt_city[0]["insight"]["facts"]
    check("city close-in share uses ≤7d buckets (0%)",
          f["close_in_share"]["value"] == "0%", f["close_in_share"]["value"])
    check("city period label says 7 days", "7 days" in f["close_in_share"]["period"],
          f["close_in_share"]["period"])

# Contract: lead_time absent → tracked but optional
print("— Contract —")
core = {
    "hotel_name": "T", "report_date": "r",
    "yesterday": {k: 1 for k in ("revenue", "revenueLY", "roomNights",
                                 "roomNightsLY", "adr", "adrLY",
                                 "occupancy", "occupancyLY")},
    "mtd": {k: 1 for k in ("revenue", "revenueLY", "occupancy", "adr", "month_name")},
    "pickup": {"last1d": {}},
    "pace": [{k: 1 for k in ("month_num", "occ", "stly", "final", "rn", "rn_stly",
                             "rn_final_ly", "rev", "rev_stly", "rev_final",
                             "adr", "adr_final_ly")}],
    "topChannels": [{"name": "x"}],
    "next7days": [{"date": "x"}],
    "pickup_daily": [{"ref_date": date.today().isoformat()}],
    "otb_by_date": [{"rn_ty": 1}],
    "current_month_remaining": {k: 1 for k in (
        "rn_remaining_otb_ty", "rev_remaining_otb_ty", "rn_remaining_stly",
        "rev_remaining_stly", "rn_remaining_final_ly", "rev_remaining_final_ly")},
}
dq = build_data_quality(dict(core), total_rooms=100)
check("missing lead_time is tracked", "lead_time" in dq["missing_fields"])
check("missing lead_time does NOT block publication", dq["complete"] is True,
      str(dq["missing_fields"]))
check("missing lead_time does NOT trigger legacy_mode", dq["legacy_mode"] is False)

dq2 = build_data_quality(dict(core, lead_time=[{"period": "TY"}]), total_rooms=100)
check("present lead_time not flagged missing", "lead_time" not in dq2["missing_fields"])
check("lead_time counted in rows_fetched", dq2["rows_fetched"].get("lead_time") == 1)

# cancel_daily (Q14) — same optional-signal contract treatment
check("missing cancel_daily is tracked", "cancel_daily" in dq["missing_fields"])
check("missing cancel_daily does NOT block publication", dq["complete"] is True)
check("missing cancel_daily does NOT trigger legacy_mode", dq["legacy_mode"] is False)
dq3 = build_data_quality(dict(core, cancel_daily=[{"ref_date": "2026-07-28"}]),
                         total_rooms=100)
check("present cancel_daily not flagged missing",
      "cancel_daily" not in dq3["missing_fields"])
check("cancel_daily counted in rows_fetched",
      dq3["rows_fetched"].get("cancel_daily") == 1)

# consumed_by_source (Q15, ADR bridge) — same optional treatment
check("missing consumed_by_source is tracked",
      "consumed_by_source" in dq["missing_fields"])
check("missing consumed_by_source does NOT block", dq["complete"] is True)
dq4 = build_data_quality(dict(core, consumed_by_source=[{"period": "TY"}]),
                         total_rooms=100)
check("present consumed_by_source not flagged missing",
      "consumed_by_source" not in dq4["missing_fields"])
check("consumed_by_source counted in rows_fetched",
      dq4["rows_fetched"].get("consumed_by_source") == 1)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
