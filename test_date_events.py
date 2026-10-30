"""
Date-level engine (briefing/date_events.py + analyst presentation), 2026-10-31.
Synthetic data only — no DB, no network, no Claude.

  engine   : cluster ahead / behind, "last year was an event", week apart from
             its month, sudden demand up / down — and the safety rules: a
             month-wide dip is NOT a date story, closed nights are never
             "behind", thin volume says nothing, size is not a cause
  cards    : every fallback inside the caps, plain words, review language
  dark     : no date card reaches the briefing; cards_audit records them
  live     : DATE_CARDS on → they compete; an overlapping soft-dates card gives way

Run:  py -3.13 test_date_events.py
"""

from datetime import date, timedelta

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

import briefing.analyst as A
from briefing import date_events as DE
from briefing.analyst import (_WORD_CAPS, _plainify_text, _action_violations, _shape_problems,
                              _fingerprint, _in_card_window, _fold_date_cards)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name} {detail}")


TODAY = date(2026, 11, 2)            # a Monday; report date = Nov 1
REPORT = "2026-11-01"


def D(m, d):
    return date(2026, m, d)


def cube(fn, extra=None):
    """91 nights from TODAY. fn(day) -> (ty, ly); split BK 60% / Direct 40%.
    extra(day) -> list of extra rows (e.g. a group source)."""
    rows = []
    for k in range(91):
        day = TODAY + timedelta(days=k)
        ty, ly = fn(day)
        for src, w in (("BK", 0.6), ("Direct", 0.4)):
            rows.append({"stay_date": day.isoformat(), "source": src, "segment": "LEI",
                         "rn_ty": ty * w, "rev_ty": ty * w * 120, "rn_ly": ly * w, "rev_ly": ly * w * 110})
        if extra:
            rows.extend(extra(day))
    return rows


def run_engine(rows, pbs=None):
    found, errs = DE.discover_dates({"booking_cube": rows, "pickup_by_source": pbs or []}, TODAY)
    return found, errs


# ── 1. cluster AHEAD, driven by a group source ──────────────────────────────
print("1. cluster ahead (Fri Nov 20–Sun Nov 22, +75%, GRP)")
ahead_nights = {D(11, 20), D(11, 21), D(11, 22)}
rows_ahead = cube(lambda d: (40, 40),
                  extra=lambda d: ([{"stay_date": d.isoformat(), "source": "GRP", "segment": "MICE",
                                     "rn_ty": 30, "rev_ty": 3000, "rn_ly": 0, "rev_ly": 0}]
                                   if d in ahead_nights else []))
found, errs = run_engine(rows_ahead)
cl = [f for f in found if f["type"] == "cluster"]
check("no errors", not errs, str(errs))
check("one cluster found", len(cl) == 1, str([(f["type"], f["from"], f["to"]) for f in found]))
if cl:
    c = cl[0]
    check("dates Nov 20–Nov 22, ahead", (c["from"], c["to"], c["sign"]) == (D(11, 20), D(11, 22), 1), str(c))
    check("gap +90 rn, +75%", round(c["gap"]) == 90 and abs(c["pct"] - 0.75) < 1e-9, str((c["gap"], c["pct"])))
    check("nights around are flat (0%)", abs(c["ctx_pct"]) < 1e-9, str(c["ctx_pct"]))
    check("GRP named (all of the lead, normally a small share)",
          c["driver"] and c["driver"]["name"] == "GRP", str(c["driver"]))
    check("this year's nights stand out from the same weekdays around (×1.75)",
          abs(c["ty_vs_nb"] - 1.75) < 1e-9, str(c["ty_vs_nb"]))
check("no week finding for a week a cluster already tells",
      not [f for f in found if f["type"] == "week"], str(found))

# ── 2. last year was an event ────────────────────────────────────────────────
print("2. last year was an event (Sat Nov 28–Sun Nov 29)")
spike = {D(11, 28), D(11, 29)}
found, _ = run_engine(cube(lambda d: (40, 80) if d in spike else (40, 40)))
ev = [f for f in found if f["type"] == "ly_event"]
check("classified as ly_event, not a slowdown", len(ev) == 1 and not [f for f in found if f["type"] == "cluster"],
      str([(f["type"], f["from"]) for f in found]))
if ev:
    check("last year ×2.0 the same weekdays around, this year normal",
          abs(ev[0]["ly_vs_nb"] - 2.0) < 1e-9 and abs(ev[0]["ty_vs_nb"] - 1.0) < 1e-9, str(ev[0]))

# ── 3. cluster BEHIND, driven by BK (not size) ───────────────────────────────
print("3. cluster behind (Tue Nov 10–Wed Nov 11)")
dip = {D(11, 10), D(11, 11)}


def dip_rows():
    rows = []
    for k in range(91):
        day = TODAY + timedelta(days=k)
        bk_ty = 4 if day in dip else 24          # BK loses 20 a night, Direct unchanged
        rows += [{"stay_date": day.isoformat(), "source": "BK", "rn_ty": bk_ty, "rev_ty": bk_ty * 120,
                  "rn_ly": 24, "rev_ly": 24 * 110},
                 {"stay_date": day.isoformat(), "source": "Direct", "rn_ty": 16, "rev_ty": 16 * 120,
                  "rn_ly": 16, "rev_ly": 16 * 110}]
    return rows


found, _ = run_engine(dip_rows())
cl = [f for f in found if f["type"] == "cluster"]
check("one behind cluster", len(cl) == 1 and cl[0]["sign"] == -1, str(found))
if cl:
    check("gap −40 rn, −50%", round(cl[0]["gap"]) == -40 and abs(cl[0]["pct"] + 0.5) < 1e-9, str(cl[0]))
    check("BK named (100% of the gap vs 60% of the nights)",
          cl[0]["driver"] and cl[0]["driver"]["name"] == "BK", str(cl[0]["driver"]))

# ── 4–6. safety: month-wide dip, closed nights, thin volume ─────────────────
print("4–6. safety rules")
found, _ = run_engine(cube(lambda d: (28, 40)))
check("a month-wide −30% is a month story: no date finding", found == [], str(found))
found, _ = run_engine(cube(lambda d: (0, 40) if d >= D(11, 15) else (40, 40)))
check("closed nights (nothing booked this year) are never 'behind'", found == [], str(found))
found, _ = run_engine(cube(lambda d: (6, 2) if d == D(11, 20) else (2, 2)))
check("thin volume (+4 rn on a 2-rn night) says nothing", found == [], str(found))
rows_none = cube(lambda d: (40, 40),
                 extra=lambda d: ([{"stay_date": d.isoformat(), "source": "(none)", "rn_ty": 30, "rev_ty": 3000,
                                    "rn_ly": 0, "rev_ly": 0}] if d in ahead_nights else []))
found, _ = run_engine(rows_none)
cl = [f for f in found if f["type"] == "cluster"]
check("an unclassified '(none)' source is never named", cl and cl[0]["driver"] is None or
      (cl and cl[0]["driver"]["key"] == "segment"), str(cl[0]["driver"] if cl else found))

# ── 7. a week apart from its month ───────────────────────────────────────────
print("7. week of Nov 16 apart from the rest of November")
week = {D(11, 16) + timedelta(days=i): (32 if i % 2 == 0 else 36) for i in range(7)}
found, _ = run_engine(cube(lambda d: (week[d], 40) if d in week else (40, 40)))
wk = [f for f in found if f["type"] == "week"]
check("no single-night clusters (each night < 10 rn)", not [f for f in found if f["type"] == "cluster"], str(found))
check("the week is found", len(wk) == 1, str(found))
if wk:
    w = wk[0]
    check("Mon Nov 16–Sun Nov 22, November, −44 rn",
          (w["from"], w["to"], w["month"], round(w["gap"])) == (D(11, 16), D(11, 22), 11, -44), str(w))
    check("rest of November flat", abs(w["rest_pct"]) < 1e-9, str(w["rest_pct"]))

# ── 8–10. sudden demand ──────────────────────────────────────────────────────
print("8–10. sudden demand")
yday = TODAY - timedelta(days=1)


def pbs_rows(prior_per_night, yday_rows, base_dates=True):
    rows = []
    for k in range(1, 8):
        ref = (yday - timedelta(days=k)).isoformat()
        for sd, rn in prior_per_night.items():
            rows.append({"ref_date": ref, "stay_date": sd.isoformat(), "source": "BK", "net_rn": rn, "net_rev": rn * 100})
        if base_dates:
            for i in range(10):
                rows.append({"ref_date": ref, "stay_date": D(12, 1 + i).isoformat(), "source": "BK",
                             "net_rn": 2, "net_rev": 200})
    for sd, src, rn in yday_rows:
        rows.append({"ref_date": yday.isoformat(), "stay_date": sd.isoformat(), "source": src,
                     "net_rn": rn, "net_rev": rn * 100})
    if base_dates:
        for i in range(10):
            rows.append({"ref_date": yday.isoformat(), "stay_date": D(12, 1 + i).isoformat(), "source": "BK",
                         "net_rn": 2, "net_rev": 200})
    return rows


surge_yday = [(D(11, 12), "GRP", 8), (D(11, 13), "GRP", 9), (D(11, 14), "GRP", 6), (D(11, 14), "BK", 1)]
normal_low = {D(11, 12): 0.3, D(11, 13): 0.4, D(11, 14): 0.3}
found, _ = run_engine([], pbs_rows(normal_low, surge_yday))
sg = [f for f in found if f["type"] == "surge"]
check("surge found for Nov 12–Nov 14", len(sg) == 1 and (sg[0]["from"], sg[0]["to"], sg[0]["sign"]) ==
      (D(11, 12), D(11, 14), 1), str(found))
if sg:
    check("24 new room nights against a normal 1.0 a day", round(sg[0]["new_rn"]) == 24
          and abs(sg[0]["normal_per_day"] - 1.0) < 1e-9, str(sg[0]))
    check("GRP named (never in last week's bookings)", sg[0]["driver"] and sg[0]["driver"]["name"] == "GRP",
          str(sg[0]["driver"]))
    check("share of yesterday's bookings ~55%", abs(sg[0]["share_of_yday"] - 24 / 44) < 1e-9, str(sg[0]))
found, _ = run_engine([], pbs_rows({D(11, 12): 10, D(11, 13): 10, D(11, 14): 10}, surge_yday))
check("no surge when those nights normally get more than that", not found, str(found))
drop = [(D(11, 20), "BK", -8), (D(11, 21), "BK", -7)]
found, _ = run_engine([], pbs_rows({}, drop))
sg = [f for f in found if f["type"] == "surge"]
check("a sudden drop is found (net −15)", len(sg) == 1 and sg[0]["sign"] == -1, str(found))
if sg:
    check("BK not named: it IS the business (size is not a cause)", sg[0]["driver"] is None, str(sg[0]["driver"]))

# ── 11. presentation: every card inside the caps, plain, review language ────
print("11. cards")
cases = {
    "cluster ahead": rows_ahead,
    "ly_event": cube(lambda d: (40, 80) if d in spike else (40, 40)),
    "cluster behind": dip_rows(),
    "week": cube(lambda d: (week[d], 40) if d in week else (40, 40)),
}
ctx = {"today": TODAY, "daily_rev_baseline": 4000.0}
cards = []
for name, rows in cases.items():
    for f in run_engine(rows)[0]:
        cards.append((name, A._date_candidate(f, ctx)))
for f in run_engine([], pbs_rows(normal_low, surge_yday))[0] + run_engine([], pbs_rows({}, drop))[0]:
    cards.append((f"surge {f['sign']:+d}", A._date_candidate(f, ctx)))
check("a card for every finding type", {c["date_type"] for _, c in cards} == {"cluster", "ly_event", "week", "surge"},
      str([c["date_type"] for _, c in cards]))
for name, c in cards:
    fb = c["fallback_card"]
    check(f"{name}: shape", not _shape_problems(fb), str(_shape_problems(fb)))
    for field, cap in _WORD_CAPS.items():
        txt = str(fb.get(field, ""))
        n = len(txt.split())
        check(f"{name}: {field} {n}<={cap}", n <= cap and not txt.endswith("…"), txt)
        _, hits = _plainify_text(txt)
        check(f"{name}: {field} plain", not hits, str(hits))
    check(f"{name}: review language", not _action_violations(fb), str(_action_violations(fb)))
    check(f"{name}: no euro in the card text", "€" not in " ".join(str(fb[k]) for k in _WORD_CAPS), str(fb))
    check(f"{name}: card window never drops it", _in_card_window(c, TODAY))
byname = {n: c for n, c in cards}
ca = byname["cluster ahead"]["fallback_card"]
check("ahead card names the nights, the weekday comparison and GRP",
      ca["headline"] == "Fri Nov 20–Sun Nov 22: well ahead of last year" and "same weekdays" in ca["what_happened"]
      and "GRP accounts for" in ca["why_it_matters"], str(ca))
check("ly_event card says 'possibly a one-off event', tag MONITOR",
      "one-off event" in byname["ly_event"]["fallback_card"]["why_it_matters"]
      and byname["ly_event"]["tag"] == "MONITOR")
check("week card names the week and the month", byname["week"]["fallback_card"]["headline"]
      == "Week of Nov 16 behind the rest of November", byname["week"]["fallback_card"]["headline"])
su = byname["surge +1"]
check("surge card: low confidence on the cause", su["insight"]["cause_hypotheses"][0]["confidence"] == "Low")

# ── 12. fingerprints ─────────────────────────────────────────────────────────
print("12. fingerprints")
fp = _fingerprint(su, REPORT)
check("surge = an event: key day:<yesterday>", fp["key"] == f"day:{REPORT}" and fp["metric"] is None, str(fp))
fp = _fingerprint(byname["cluster ahead"], REPORT)
check("cluster: key = its nights, metric = its room-night move (floor 5)",
      fp["key"] == "date:2026-11-20|date:2026-11-21|date:2026-11-22" and round(fp["metric"]) == 90
      and fp["floor"] == 5.0, str(fp))

# ── 13. dark by default: nothing reaches the briefing; audit records it ─────
print("13. dark launch")
payload = {"hotel_name": "Test Hotel", "report_date_iso": REPORT, "pace": [], "mtd": {},
           "current_month_remaining": {}, "yesterday": {}, "sources_by_month": [],
           "booking_cube": rows_ahead, "pickup_by_source": pbs_rows(normal_low, surge_yday)}
check("DATE_CARDS is off by default", A._DATE_CARDS is False)
out = A._compute_signals(payload)
allc = out["ranked"] + out["watchlist"] + out.get("demoted", [])
check("date findings computed", len(out["date_cards"]) >= 2, str([c["insight"]["id"] for c in out["date_cards"]]))
check("no date card among the candidates", not [c for c in allc if c["signal"] == "date_event"])
res = A.generate_insights(payload, narrate=False)
audit = [a for a in res["_meta"]["cards_audit"] if str(a.get("card_id", "")).startswith("dates:")]
check("cards_audit records them as WATCH", audit and all(a["tag"] == "WATCH" for a in audit), str(audit)[:300])
check("audit facts carry no internal euro estimate",
      all(not k.startswith("value_at_stake") for a in audit for k in a["facts"]))
check("no date card in the published insights",
      not [i for i in res.get("insights", []) if str(i.get("id", "")).startswith("date_")])

# ── 14. live mode ─────────────────────────────────────────────────────────────
print("14. live mode (DATE_CARDS=1)")
A._DATE_CARDS = True
try:
    out = A._compute_signals(payload)
    allc = out["ranked"] + out["watchlist"] + out.get("demoted", [])
    check("date cards compete when live", [c for c in allc if c["signal"] == "date_event"])
    soft_on = {"signal": "soft_dates", "insight": {"stay_period": {"from": "2026-11-19", "to": "2026-11-24"}}}
    soft_off = {"signal": "soft_dates", "insight": {"stay_period": {"from": "2026-12-05", "to": "2026-12-09"}}}
    kept = _fold_date_cards([soft_on, soft_off], [byname["cluster ahead"], byname["week"], su])
    check("a soft-dates card on the same nights gives way, another stays",
          soft_on not in kept and soft_off in kept, str([k.get("signal") for k in kept]))
    check("at most 2 date cards a day", sum(1 for k in kept if k.get("signal") == "date_event") == 2)
finally:
    A._DATE_CARDS = False

# ── 15. fail-open ─────────────────────────────────────────────────────────────
print("15. fail-open")
found, errs = DE.discover_dates({"booking_cube": [{"stay_date": "garbage"}, "x"], "pickup_by_source": [{}]}, TODAY)
check("malformed rows: no crash, no findings", found == [], str((found, errs)))
out = A._compute_signals(dict(payload, booking_cube=None, pickup_by_source=None))
check("missing inputs: briefing computes, no date cards", out["date_cards"] == [])

# ── 16. fast-filling nights (user 2026-10-31, "only when it happens") ───────
print("16. fast-filling nights")
FRI, SAT = D(11, 20), D(11, 21)                 # 18 days out
weekend = {FRI, SAT}


def fill_pbs(extra=None, yday_extra=None):
    """Q21: every night of the horizon gets 1 rn on 2 of the last 7 days (usual
    7-day net = 2). extra = {night: (source, rn a day on yday-6..yday-1)},
    yday_extra = {night: (source, rn yesterday)}; an 8th, older booking day
    (50 rn) must never count."""
    rows = []
    for k in range(90):
        n = (TODAY + timedelta(days=k)).isoformat()
        for ref in (yday - timedelta(days=6), yday - timedelta(days=3)):
            rows.append({"ref_date": ref.isoformat(), "stay_date": n, "source": "BK", "net_rn": 1, "net_rev": 120})
    for n, (src, rn) in (extra or {}).items():
        rows.append({"ref_date": (yday - timedelta(days=7)).isoformat(), "stay_date": n.isoformat(),
                     "source": src, "net_rn": 50, "net_rev": 5000})
        for k in range(1, 7):
            rows.append({"ref_date": (yday - timedelta(days=k)).isoformat(), "stay_date": n.isoformat(),
                         "source": src, "net_rn": rn, "net_rev": rn * 130})
    for n, (src, rn) in (yday_extra or {}).items():
        rows.append({"ref_date": yday.isoformat(), "stay_date": n.isoformat(), "source": src,
                     "net_rn": rn, "net_rev": rn * 130})
    return rows


def run_fill(rows, pbs, rooms=36):
    data = {"booking_cube": rows, "pickup_by_source": pbs}
    if rooms is not None:
        data["total_rooms"] = rooms
    return DE.discover_dates(data, TODAY)


grp_week = {FRI: ("GRP", 2), SAT: ("GRP", 2)}
grp_yday = {FRI: ("GRP", 1), SAT: ("GRP", 1)}
cube_wknd = cube(lambda d: (26, 15) if d in weekend else (15, 15))
found, errs = run_fill(cube_wknd, fill_pbs(grp_week, grp_yday))
fl = [f for f in found if f["type"] == "fill"]
check("no errors", not errs, str(errs))
check("one fill finding Fri Nov 20–Sat Nov 21", len(fl) == 1 and (fl[0]["from"], fl[0]["to"], fl[0]["sign"])
      == (FRI, SAT, 1), str([(f["type"], f["from"], f["to"]) for f in found]))
if fl:
    f = fl[0]
    check("30 rn in 7 days vs a usual 4 (2 a night); the older 8th day ignored",
          round(f["new_rn"]) == 30 and abs(f["usual_rn"] - 4) < 1e-9, str((f["new_rn"], f["usual_rn"])))
    check("20 of 72 room nights still free, 2 of them booked yesterday",
          (round(f["free"]), round(f["capacity"]), round(f["yday_rn"])) == (20, 72, 2), str(f))
    check("same-weekday comparison carried: 52 vs 30", (round(f["ty"]), round(f["ly"])) == (52, 30), str(f))
    check("GRP named (26 of 30, normally a small share)", f["driver"] and f["driver"]["name"] == "GRP",
          str(f["driver"]))
check("one story per night: the 'ahead' cluster on the same nights gives way",
      not [c for c in found if c["type"] == "cluster" and set(c["dates"]) & weekend], str(found))

found, _ = run_fill(cube(lambda d: (15, 15)), fill_pbs({D(11, 7): ("GRP", 2)}, {D(11, 7): ("GRP", 1)}))
check("5 days out is the normal last-minute flow: no fill", not [f for f in found if f["type"] == "fill"], str(found))
found, _ = run_fill(cube(lambda d: (15, 15)), fill_pbs(None, {FRI: ("GRP", 13), SAT: ("GRP", 13)}))
check("mostly yesterday: no fill, the surge tells it",
      not [f for f in found if f["type"] == "fill"] and [f for f in found if f["type"] == "surge"], str(found))
found, _ = run_fill(cube(lambda d: (36, 15) if d in weekend else (15, 15)), fill_pbs(grp_week, grp_yday))
check("sold-out nights: nothing left to fill", not [f for f in found if f["type"] == "fill"], str(found))
tue_wed = {D(11, 10): ("GRP", 2), D(11, 11): ("GRP", 2)}
found, _ = run_fill(cube(lambda d: (5, 15) if d in tue_wed else (15, 15)),
                    fill_pbs(tue_wed, {d: ("GRP", 1) for d in tue_wed}))
check("62 rooms free 8 days out at ~4 a day: they would outlast the nights, no fill",
      not [f for f in found if f["type"] == "fill"], str(found))
everyone = {TODAY + timedelta(days=k): ("BK", 2) for k in range(90)}
found, _ = run_fill(cube(lambda d: (15, 15)), fill_pbs(everyone))
check("every night equally fast: nothing stands out, no fill", not [f for f in found if f["type"] == "fill"],
      str(found))
found, _ = run_fill(cube_wknd, fill_pbs(grp_week, grp_yday), rooms=None)
check("no room count: silent", not [f for f in found if f["type"] == "fill"], str(found))
found, _ = run_fill(cube_wknd, fill_pbs(grp_week, grp_yday), rooms=200)
check("200-room hotel: 15 a night is under 8% of the rooms, no fill",
      not [f for f in found if f["type"] == "fill"], str(found))
found, errs = run_fill([], fill_pbs(grp_week, grp_yday) + [{"ref_date": "x", "stay_date": None}, {}])
check("no cube, malformed Q21 rows: no crash", not errs, str(errs))

fill_f = [f for f in run_fill(cube_wknd, fill_pbs(grp_week, grp_yday))[0] if f["type"] == "fill"][0]
fc = A._date_candidate(fill_f, ctx)
fb = fc["fallback_card"]
check("fill card: shape", not _shape_problems(fb), str(_shape_problems(fb)))
for field, cap in _WORD_CAPS.items():
    txt = str(fb.get(field, ""))
    n = len(txt.split())
    check(f"fill card: {field} {n}<={cap}", n <= cap and not txt.endswith("…"), txt)
    _, hits = _plainify_text(txt)
    check(f"fill card: {field} plain", not hits, str(hits))
check("fill card: review language", not _action_violations(fb), str(_action_violations(fb)))
check("fill card: no euro in the card text", "€" not in " ".join(str(fb[k]) for k in _WORD_CAPS), str(fb))
check("fill card: headline, tag, id", (fb["headline"], fc["tag"], fc["insight"]["id"])
      == ("Fri Nov 20–Sat Nov 21 filling fast", "OPPORTUNITY", "date_fill_nov_2026"), str((fb["headline"], fc["tag"])))
check("fill card: the card's numbers", fb["evidence"][0]["value"] == "+30 rn" and fb["evidence"][1]["value"] == "20"
      and "30 room nights booked in the last 7 days" in fb["what_happened"] and "usual 4" in fb["what_happened"]
      and "20 of 72" in fb["what_happened"], str(fb))
check("fill card: GRP and the same-weekday comparison in the why",
      "GRP accounts for" in fb["why_it_matters"] and "+73.3% vs the same weekdays last year" in fb["why_it_matters"],
      fb["why_it_matters"])
check("fill card: sell-out worded as a condition, no date or count",
      "would be booked before these nights arrive" in fb["why_it_matters"])
check("fill card: confidence Medium", fc["insight"]["cause_hypotheses"][0]["confidence"] == "Medium")
check("fill card: by when within 3 days (18 days out)", fb["by_when"] == "Within 3 days.", fb["by_when"])
check("fill card: card window never drops it", _in_card_window(fc, TODAY))
fp = _fingerprint(fc, REPORT)
check("fill fingerprint: key = its nights, metric = rooms booked on them (floor 5)",
      fp["key"] == "date:2026-11-20|date:2026-11-21" and round(fp["metric"]) == 52 and fp["floor"] == 5.0, str(fp))
out = A._compute_signals(dict(payload, booking_cube=cube_wknd, pickup_by_source=fill_pbs(grp_week, grp_yday),
                              total_rooms=36))
check("through the analyst: fill computed, dark by default",
      "date_fill_nov_2026" in [c["insight"]["id"] for c in out["date_cards"]]
      and not [c for c in out["ranked"] + out["watchlist"] + out.get("demoted", []) if c["signal"] == "date_event"],
      str([c["insight"]["id"] for c in out["date_cards"]]))

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
