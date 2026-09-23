"""
Source attribution on pace cards (Q17 sources_by_month) — compute-layer tests.
No DB, no Claude.

Run:  py -3.13 test_source_attr.py
"""

import calendar
from datetime import date

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

from briefing.analyst import _compute_signals, _source_attribution, _WORD_CAPS, _plainify_text

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
# pace works on this year's months >= today.month; use next month (December → current)
NM = today.month + 1 if today.month < 12 else today.month
NM_ABBR = calendar.month_abbr[NM]


def pace_month(rn, rn_stly, adr=150.0, adr_final_ly=150.0):
    return {"month": NM_ABBR, "month_num": NM, "rn": rn, "rn_stly": rn_stly,
            "rn_final_ly": 600, "rev": rn * adr, "rev_stly": rn_stly * adr_final_ly,
            "rev_final": 90_000, "adr": adr, "adr_stly": adr_final_ly,
            "adr_final_ly": adr_final_ly, "occ": rn / 3000, "stly": rn_stly / 3000, "final": 0.8}


def src(name, rn_ty, rn_stly):
    return {"stay_month": NM, "stay_year": today.year, "source": name,
            "rn_ty": rn_ty, "rev_ty": rn_ty * 150.0, "rn_stly": rn_stly, "rev_stly": rn_stly * 150.0}


def run(pace, sources):
    out = _compute_signals({"pace": pace, "sources_by_month": sources,
                            "mtd": {}, "current_month_remaining": {}})
    return [c for c in out["ranked"] + out["watchlist"] if c["signal"] == "pace"]


def caps_ok(fb):
    for field, cap in _WORD_CAPS.items():
        txt = str(fb.get(field, ""))
        n = len(txt.split())
        check(f"  {fb['id']} {field} {n}<={cap}", n <= cap and not txt.endswith("…"), txt)
        _, hits = _plainify_text(txt)
        check(f"  {fb['id']} {field} plain", not hits, str(hits))


# ── A. Behind, concentrated in one source ────────────────────────────────────
print("A. behind — concentrated")
cards = run([pace_month(300, 400)],
            [src("Booking.com", 100, 180), src("Expedia", 80, 90), src("Direct", 120, 130)])
check("pace card fires", len(cards) == 1)
if cards:
    c = cards[0]
    f = c["insight"]["facts"]
    check("tag ALERT", c["tag"] == "ALERT")
    check("biggest_source_move", f.get("biggest_source_move", {}).get("value") == "Booking.com −80 rn", str(f.get("biggest_source_move")))
    check("biggest_source_share", f.get("biggest_source_share", {}).get("value") == "about 80% of the gap", str(f.get("biggest_source_share")))
    check("direct_bookings_move", f.get("direct_bookings_move", {}).get("value") == "Direct −10 rn", str(f.get("direct_bookings_move")))
    check("sources moving same way = 3", f.get("sources_moving_same_way", {}).get("value") == "3 sources", str(f.get("sources_moving_same_way")))
    check("period label kept", f["biggest_source_move"]["period"] == f["pace_vs_stly"]["period"])
    h = c["insight"]["cause_hypotheses"][0]
    check("hypothesis Medium, names the source", h["confidence"] == "Medium" and "Booking.com" in h["text"], str(h))
    check("directive → investigate_source", c["insight"]["action_directives"]["type"] == "investigate_source")
    fb = c["fallback_card"]
    check("fallback evidence[1] = BIGGEST MOVER", fb["evidence"][1]["label"] == "BIGGEST MOVER" and fb["evidence"][1]["value"] == "Booking.com −80 rn", str(fb["evidence"][1]))
    check("fallback why names the source", "Booking.com" in fb["why_it_matters"] and "Medium" in fb["why_it_matters"], fb["why_it_matters"])
    check("fallback action is soft and names the source", fb["recommended_action"].startswith("It may be worth") and "Booking.com" in fb["recommended_action"], fb["recommended_action"])
    caps_ok(fb)

# ── B. Behind, spread across four sources ────────────────────────────────────
print("B. behind — broad")
cards = run([pace_month(300, 400)],
            [src("A", 70, 100), src("B", 70, 100), src("C", 75, 100), src("D", 85, 100)])
check("pace card fires", len(cards) == 1)
if cards:
    c = cards[0]
    f = c["insight"]["facts"]
    check("share ~30%", f.get("biggest_source_share", {}).get("value") == "about 30% of the gap", str(f.get("biggest_source_share")))
    h = c["insight"]["cause_hypotheses"][0]
    check("hypothesis: spread across 4 sources, Medium", "spread across 4 sources" in h["text"] and h["confidence"] == "Medium", str(h))
    check("directive stays open_promo", c["insight"]["action_directives"]["type"] == "open_promo")
    check("no direct fact when no Direct source", "direct_bookings_move" not in f)
    caps_ok(c["fallback_card"])

# ── C. Sources do not reconcile with the pace gap → say nothing ──────────────
print("C. non-reconciling")
cards = run([pace_month(300, 400)],
            [src("Booking.com", 100, 120), src("Direct", 120, 140)])   # sum −40 vs gap −100
check("pace card fires", len(cards) == 1)
if cards:
    c = cards[0]
    f = c["insight"]["facts"]
    check("no attribution facts", "biggest_source_move" not in f, str(list(f)))
    check("original Low hypothesis kept", c["insight"]["cause_hypotheses"][0]["confidence"] == "Low")
    check("fallback evidence[1] = ROOM NIGHTS", c["fallback_card"]["evidence"][1]["label"] == "ROOM NIGHTS")

# ── D. Ahead, concentrated (no rate dilution) ────────────────────────────────
print("D. ahead — concentrated")
cards = run([pace_month(500, 400, adr=160.0)],
            [src("Direct", 300, 210), src("OTA", 200, 190)])
check("pace card fires", len(cards) == 1)
if cards:
    c = cards[0]
    f = c["insight"]["facts"]
    check("tag OPPORTUNITY", c["tag"] == "OPPORTUNITY")
    check("share ~90% of the lead", f.get("biggest_source_share", {}).get("value") == "about 90% of the lead", str(f.get("biggest_source_share")))
    h = c["insight"]["cause_hypotheses"][0]
    check("hypothesis[0]: Direct driving the lead, Medium", "Direct" in h["text"] and "lead" in h["text"] and h["confidence"] == "Medium", str(h))
    check("original pricing hypothesis kept as #2", len(c["insight"]["cause_hypotheses"]) == 2)
    fb = c["fallback_card"]
    check("fallback why names Direct", fb["why_it_matters"].startswith("Direct is driving"), fb["why_it_matters"])
    check("fallback evidence unchanged (REVENUE LEAD)", fb["evidence"][1]["label"] == "REVENUE LEAD")
    caps_ok(fb)

# ── E. Q17 absent → unchanged behaviour ──────────────────────────────────────
print("E. no sources_by_month")
cards = run([pace_month(300, 400)], [])
check("pace card fires without Q17", len(cards) == 1)
if cards:
    check("no attribution facts", "biggest_source_move" not in cards[0]["insight"]["facts"])

# ── F. Helper edge cases ─────────────────────────────────────────────────────
print("F. helper edges")
check("zero gap → None", _source_attribution([src("A", 10, 10)], NM, 0) is None)
check("thin mover (< 5 rn) → None", _source_attribution([src("A", 96, 100)], NM, -4) is None)
check("wrong month → None", _source_attribution([src("A", 50, 100)], NM + 1 if NM < 12 else 1, -50) is None)
check("nearly all", _source_attribution([src("A", 50, 100), src("B", 52, 50)], NM, -48)["share_txt"] == "nearly all of the gap")
check("share cap: other sources offsetting → nearly all", _source_attribution([src("A", 20, 100), src("B", 80, 50)], NM, -50)["share_txt"] == "nearly all of the gap")

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
