"""
Signal 6 (cancellation spike) + pickup cancellation attribution — compute-layer
tests. No DB, no Claude.

Run:  py -3.13 test_cancel.py
"""

import calendar
from datetime import date, timedelta

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

from briefing.analyst import _compute_signals, _WORD_CAPS, _plainify_text

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
yday  = today - timedelta(days=1)
# next month (falls back to the current month in December so the year is stable)
if today.month < 12:
    NM, NY = today.month + 1, today.year
else:
    NM, NY = today.month, today.year
NM_ABBR = calendar.month_abbr[NM]
# a finished month (last month; January → use December last year)
PM, PY = (today.month - 1, today.year) if today.month > 1 else (12, today.year - 1)


def day(i):
    """ISO date i days before yesterday (0 = yesterday)."""
    return (yday - timedelta(days=i)).isoformat()


def cancel_rows(sm, sy, prior, yday_rn, yday_rev):
    """prior = list of 7 cancel counts for yday-7 .. yday-1 (zeros are omitted,
    exactly like Q14 which only ships days WITH cancellations)."""
    rows = []
    for i, c in enumerate(prior):
        d = 7 - i
        if c:
            rows.append({"ref_date": day(d), "stay_month": sm, "stay_year": sy,
                         "cancel_rn": c, "cancel_rev": c * 300.0})
    if yday_rn:
        rows.append({"ref_date": day(0), "stay_month": sm, "stay_year": sy,
                     "cancel_rn": yday_rn, "cancel_rev": yday_rev})
    return rows


def pickup_rows(sm, sy, prior_net, yday_net, adr=150.0):
    rows = []
    for i, n in enumerate(prior_net):
        d = len(prior_net) - i
        rows.append({"ref_date": day(d), "stay_month": sm, "stay_year": sy,
                     "net_rn": n, "net_rev": n * adr})
    rows.append({"ref_date": day(0), "stay_month": sm, "stay_year": sy,
                 "net_rn": yday_net, "net_rev": yday_net * adr})
    return rows


def caps_ok(fb):
    for field, cap in _WORD_CAPS.items():
        txt = str(fb.get(field, ""))
        n = len(txt.split())
        check(f"  {fb['id']} {field} {n}<={cap}", n <= cap and not txt.endswith("…"), txt)
        _, hits = _plainify_text(txt)
        check(f"  {fb['id']} {field} plain", not hits, str(hits))


def run(pickup_daily, cancel_daily):
    return _compute_signals({"pickup_daily": pickup_daily, "cancel_daily": cancel_daily,
                             "pace": [], "mtd": {}, "current_month_remaining": {}})


def all_cands(out):
    return out["ranked"] + out["watchlist"]


# ── A. Spike fires (z >> 2, 8 rn, €2,400) — pickup stays quiet ───────────────
print("A. spike fires")
out = run(pickup_rows(NM, NY, [5] * 7, 5),
          cancel_rows(NM, NY, [1, 0, 2, 1, 0, 1, 1], 8, 2400.0))
cx = [c for c in all_cands(out) if c["signal"] == "cancellation"]
check("one cancellation candidate", len(cx) == 1, str([c["insight"]["id"] for c in all_cands(out)]))
if cx:
    c = cx[0]
    f = c["insight"]["facts"]
    check("id", c["insight"]["id"] == f"cancel_{NM_ABBR.lower()}_{NY}", c["insight"]["id"])
    check("tag ALERT", c["tag"] == "ALERT")
    check("cancelled_yday 8 rn", f["cancelled_yday"]["value"] == "8 rn", str(f["cancelled_yday"]))
    check("cancel_avg_prior 0.9 rn/day", f["cancel_avg_prior"]["value"] == "0.9 rn/day", str(f["cancel_avg_prior"]))
    check("new bookings before cancels 13 rn", f["new_bookings_yday"]["value"] == "13 rn", str(f["new_bookings_yday"]))
    check("cancelled revenue is the real €2,400", f["cancelled_rev_yday"]["value"] == "€2,400", str(f["cancelled_rev_yday"]))
    check("stake = cancelled revenue", c["stake_eur"] == 2400.0, str(c["stake_eur"]))
    check("ranked (score >= 0.08)", c in out["ranked"], str(c["score"]))
    check("hypothesis is Low confidence", c["insight"]["cause_hypotheses"][0]["confidence"] == "Low")
    check("soft action", c["fallback_card"]["recommended_action"].startswith("It may be worth"))
    check("evidence has 2 rows", len(c["fallback_card"]["evidence"]) == 2)
    caps_ok(c["fallback_card"])
pk = [c for c in all_cands(out) if c["signal"] == "pickup"]
check("pickup did not fire (flat net)", not pk, str([c["insight"]["id"] for c in pk]))

# ── B. Below the z gate → no card ────────────────────────────────────────────
print("B. below gate")
out = run([], cancel_rows(NM, NY, [3] * 7, 4, 1200.0))
check("no card at z=1", not [c for c in all_cands(out) if c["signal"] == "cancellation"])

# ── C. Volume floor: 2 rn is noise even at z >= 2 ────────────────────────────
print("C. volume floor")
out = run([], cancel_rows(NM, NY, [0] * 7, 2, 3000.0))
check("no card below 3 rn", not [c for c in all_cands(out) if c["signal"] == "cancellation"])

# ── D. Stake floor: 5 rn but only €600 ───────────────────────────────────────
print("D. stake floor")
out = run([], cancel_rows(NM, NY, [0] * 7, 5, 600.0))
check("no card below €1,000", not [c for c in all_cands(out) if c["signal"] == "cancellation"])

# ── E. Finished month is never a card ────────────────────────────────────────
print("E. finished month")
out = run([], cancel_rows(PM, PY, [0] * 7, 6, 3000.0))
check("no card for a finished month", not [c for c in all_cands(out) if c["signal"] == "cancellation"])

# ── F. Pickup ALERT + spike, same month → one card carrying the answer ────────
print("F. merge into pickup ALERT — mostly cancellations")
out = run(pickup_rows(NM, NY, [10] * 7, -2),
          cancel_rows(NM, NY, [1] * 7, 9, 2700.0))
cands = all_cands(out)
check("no standalone cancellation card", not [c for c in cands if c["signal"] == "cancellation"],
      str([c["insight"]["id"] for c in cands]))
pk = [c for c in cands if c["signal"] == "pickup"]
check("pickup ALERT present", len(pk) == 1 and pk[0]["tag"] == "ALERT")
if pk:
    c = pk[0]
    f = c["insight"]["facts"]
    check("pickup carries cancellations_yday 9 rn", f.get("cancellations_yday", {}).get("value") == "9 rn", str(f.get("cancellations_yday")))
    check("pickup carries cancellations_avg 1.0 rn/day", f.get("cancellations_avg", {}).get("value") == "1.0 rn/day", str(f.get("cancellations_avg")))
    check("gross new bookings 7 rn", f.get("new_bookings_before_cancellations", {}).get("value") == "7 rn", str(f.get("new_bookings_before_cancellations")))
    check("spike facts folded in", "cancel_cancelled_rev_yday" in f, str(list(f)))
    h = c["insight"]["cause_hypotheses"][0]
    check("hypothesis: mostly cancellations, Medium", h["text"].startswith("Mostly cancellations") and h["confidence"] == "Medium", str(h))
    fb = c["fallback_card"]
    check("fallback evidence row 2 = CANCELLED", fb["evidence"][1]["label"] == "CANCELLED", str(fb["evidence"][1]))
    check("fallback why names the cancellations", "9 rn cancelled yesterday" in fb["why_it_matters"], fb["why_it_matters"])
    caps_ok(fb)

# ── G. Pickup ALERT with normal cancellations → "new bookings slowed" ─────────
print("G. pickup ALERT — cancellations normal")
out = run(pickup_rows(NM, NY, [10] * 7, -2),
          cancel_rows(NM, NY, [1] * 7, 1, 300.0))
pk = [c for c in all_cands(out) if c["signal"] == "pickup"]
check("pickup ALERT present", len(pk) == 1)
if pk:
    c = pk[0]
    h = c["insight"]["cause_hypotheses"][0]
    check("hypothesis: new bookings slowed, Medium", h["text"].startswith("New bookings slowed") and h["confidence"] == "Medium", str(h))
    check("directive → investigate demand", c["insight"]["action_directives"]["type"] == "investigate_demand")
    fb = c["fallback_card"]
    check("fallback evidence row 2 stays REVENUE IMPACT", fb["evidence"][1]["label"] == "REVENUE IMPACT")
    caps_ok(fb)
check("no cancellation card at 1 rn", not [c for c in all_cands(out) if c["signal"] == "cancellation"])

# ── H. No Q14 data at all → pickup unchanged, nothing breaks ─────────────────
print("H. cancel_daily absent")
out = run(pickup_rows(NM, NY, [10] * 7, -2), [])
pk = [c for c in all_cands(out) if c["signal"] == "pickup"]
check("pickup ALERT present without Q14", len(pk) == 1)
if pk:
    f = pk[0]["insight"]["facts"]
    check("no cancellation facts without Q14", "cancellations_yday" not in f)
    check("original Low hypothesis kept", pk[0]["insight"]["cause_hypotheses"][0]["confidence"] == "Low")

# ── I. Cap: at most 2 cancellation cards per day ─────────────────────────────
print("I. per-day cap")
months = [(NM, NY)]
m, y = NM, NY
for _ in range(3):
    m, y = (m + 1, y) if m < 12 else (1, y + 1)
    months.append((m, y))
rows = []
for sm, sy in months:
    rows += cancel_rows(sm, sy, [0] * 7, 6, 3000.0)
out = run([], rows)
cx = [c for c in all_cands(out) if c["signal"] == "cancellation"]
check("4 spiking months → 2 cards", len(cx) == 2, str([c["insight"]["id"] for c in cx]))

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
