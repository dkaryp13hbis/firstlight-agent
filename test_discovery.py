"""
Release 2 — cross-filter discovery engine (briefing/discovery.py) on
synthetic cubes. No DB, no network, no Claude.

  T1  month −3% overall, weekend nights −57% hidden by weekdays → found, and
      the weekend gap is traced to one source × booking window
  T4' month −26%: one source × booking window carries it → named, reconciles
  T6  offsetting sources → signed contributions, "more than the whole gap"
  T7  tiny slice with a huge % → suppressed
  T-noise  a gap spread evenly across many slices → nothing (no hunting)
  T3' yesterday's cancellations concentrated in one source + dates
  T9  dark launch: recorded in cards_audit, never a card; live: folds into
      the month's existing card (one story, one card)
  Opera cube from grain rows (same-weekday LY, cancelled-later restored);
  assemble pass-through; publish strip; contract optional.

Run:  py -3.13 test_discovery.py
"""

import json
from datetime import date, timedelta

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

import briefing.analyst as A
from briefing import discovery as D
from briefing.analyst import (_WORD_CAPS, _plainify_text, _action_violations, _shape_problems,
                              _compute_signals, _discovery_candidate, _signal_context)

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


TODAY = date(2026, 10, 28)
REPORT = "2026-10-27"


def row(d, src, bucket, ty, ly, rate=150.0, cat=None):
    return {"stay_date": d.isoformat(), "source": src, "room_cat": cat, "lead_bucket": bucket,
            "rn_ty": ty, "rev_ty": ty * rate, "rn_ly": ly, "rev_ly": ly * rate}


def month_days(y, m):
    d = date(y, m, 1)
    while d.month == m:
        yield d
        d += timedelta(days=1)


def template_ok(fb, label):
    check(f"{label}: shape", not _shape_problems(fb), str(_shape_problems(fb)))
    for field, cap in _WORD_CAPS.items():
        txt = str(fb.get(field, ""))
        n = len(txt.split())
        check(f"{label}: {field} {n}<={cap}", n <= cap and not txt.endswith("…"), txt)
        _, hits = _plainify_text(txt)
        check(f"{label}: {field} plain", not hits, str(hits))
    check(f"{label}: review language", not _action_violations(fb), str(_action_violations(fb)))


# ── T1: hidden weekend weakness ──────────────────────────────────────────────
print("T1. hidden weekend weakness (same weekdays last year)")
cube_t1 = []
for d in month_days(2026, 11):
    if d.weekday() in (4, 5):    # Fri / Sat nights: BK has vanished
        cube_t1 += [row(d, "BK", "31-60", 3, 18), row(d, "Direct", "16-30", 10, 12)]
    else:
        cube_t1 += [row(d, "BK", "16-30", 15, 12), row(d, "Direct", "16-30", 15, 13)]
found = D.hidden_day_type(cube_t1, TODAY, 1000.0)
check("one hidden discovery", len(found) == 1, str(found))
check("no gap driver (month only −3%)", not D.gap_drivers(cube_t1, TODAY, 1000.0))
if found:
    h = found[0]
    check("weekend is the hidden slice", h["pattern"]["dims"] == {"day_type": "weekend"})
    check("month −3.3%", round(h["overall"]["pct"] * 100, 1) == -3.3, str(h["overall"]))
    check("weekend −56.7%", round(h["pattern"]["pct"] * 100, 1) == -56.7, str(h["pattern"]))
    check("signed contributions reconcile", h["reconciles"]
          and abs(sum(c["delta"] for c in h["contributions"]) - h["overall"]["delta"]) < 1e-9)
    check("weekday nights cover it (+110 rn)", h["contributions"][1]["delta"] == 110, str(h["contributions"]))
    drv = h["driver"]
    check("weekend gap traced to BK × 31–60 days", drv and drv["dims"] == {"source": "BK", "lead_bucket": "31-60"},
          str(drv and drv["dims"]))
    check("BK carries 120 of the 136 rn", drv and drv["delta"] == -120 and abs(drv["share"] - 120 / 136) < 1e-9)
    check("driver reconciles with the weekend gap", drv and drv["reconciles"])
    ctx = _signal_context({"report_date_iso": REPORT, "pace": []})
    cand = _discovery_candidate(h, ctx)
    fb = cand["fallback_card"]
    check("headline", fb["headline"] == "Nov: weekend bookings 57% behind, hidden by weekdays", fb["headline"])
    check("names BK verbatim", "BK bookings made 31–60 days ahead" in fb["why_it_matters"], fb["why_it_matters"])
    check("confidence High", cand["insight"]["cause_hypotheses"][0]["confidence"] == "High")
    check("stake is internal only", "value_at_stake" in cand["insight"]["facts"] and "€" not in fb["why_it_matters"])
    template_ok(fb, "hidden_weekend")

# ── T4': a month behind, carried by one slice ───────────────────────────────
print("T4'. gap driver: BK bookings made 31–60 days ahead")
cube_t4 = []
for d in month_days(2026, 12):
    cube_t4 += [row(d, "Direct", "16-30", 20, 20), row(d, "BK", "31-60", 5, 15), row(d, "BK", "0-3", 3, 3)]
gd = D.gap_drivers(cube_t4, TODAY, 1000.0)
check("one gap driver", len(gd) == 1, str(gd))
if gd:
    g = gd[0]
    check("slice = BK × 31–60", g["pattern"]["dims"] == {"source": "BK", "lead_bucket": "31-60"}, str(g["pattern"]["dims"]))
    check("carries the whole gap", abs(g["share"] - 1.0) < 1e-9, str(g["share"]))
    check("rest of the month flat", abs(g["rest_pct"]) < 1e-9)
    check("contributions reconcile", g["reconciles"])
    check("more slices tested than one", g["tested"] > 5, str(g["tested"]))
    check("dims in canonical order (stable card id)", list(g["pattern"]["dims"]) == ["source", "lead_bucket"],
          str(list(g["pattern"]["dims"])))
    cand = _discovery_candidate(g, _signal_context({"report_date_iso": REPORT, "pace": []}))
    fb = cand["fallback_card"]
    check("headline", fb["headline"] == "Dec gap: mostly BK bookings made 31–60 days ahead", fb["headline"])
    check("says the rest is close to last year", "close to last year" in fb["why_it_matters"], fb["why_it_matters"])
    template_ok(fb, "gap_driver")

# ── T6: offsetting sources ───────────────────────────────────────────────────
print("T6. offsetting sources")
cube_t6 = []
for d in month_days(2026, 12):   # month 837 vs 961 rn = −12.9%
    cube_t6 += [row(d, "BK", "16-30", 3, 10), row(d, "Direct", "16-30", 14, 11), row(d, "OTA", "16-30", 10, 10)]
gd = D.gap_drivers(cube_t6, TODAY, 1000.0)
check("driver found", len(gd) == 1, str(gd))
if gd:
    g = gd[0]
    check("BK −217 rn against a −124 rn month", g["pattern"]["delta"] == -217 and g["overall"]["delta"] == -124,
          str((g["pattern"]["delta"], g["overall"]["delta"])))
    check("signed rest is +93 (Direct offsets)", g["contributions"][1]["delta"] == 93, str(g["contributions"]))
    cand = _discovery_candidate(g, _signal_context({"report_date_iso": REPORT, "pace": []}))
    check("share said in words", cand["insight"]["facts"]["slice_share"]["value"] ==
          "more than the whole gap, partly offset by other sources", str(cand["insight"]["facts"]["slice_share"]))

# ── T7: tiny slice ───────────────────────────────────────────────────────────
print("T7. tiny slice, huge percentage")
cube_t7 = []
for i, d in enumerate(month_days(2026, 12)):
    cube_t7 += [row(d, "Direct", "16-30", 18, 20), row(d, "OTA", "16-30", 18, 20),
                row(d, "Mice", "61-90", 0 if i < 3 else 0, 1 if i < 10 else 0)]
check("tiny Mice slice never explains the gap", not any(
    g["pattern"]["dims"].get("source") == "Mice" for g in D.gap_drivers(cube_t7, TODAY, 1000.0)))

# ── T-noise: gap spread evenly — nothing to name ────────────────────────────
print("T-noise. even spread across many slices")
cube_n = []
for d in month_days(2026, 12):
    for src in ("A", "B", "C", "D", "E", "F"):
        for b in ("0-3", "16-30", "61-90"):
            cube_n.append(row(d, src, b, 4, 5))
check("no slice carries >= 60% → silent", not D.gap_drivers(cube_n, TODAY, 1000.0))
check("z bar rises with slices tested", D.z_crit(200) > D.z_crit(10) >= 2.0)

# ── T3': cancellation concentration ─────────────────────────────────────────
print("T3'. cancellations concentrated in one source")
yday = TODAY - timedelta(days=1)
det = [{"cancel_date": yday.isoformat(), "stay_date": f"2026-11-{d}", "source": "TO", "cancel_rn": 5, "cancel_rev": 750}
       for d in (12, 13, 14)]
det.append({"cancel_date": yday.isoformat(), "stay_date": "2026-12-02", "source": "Direct", "cancel_rn": 2, "cancel_rev": 300})
det += [{"cancel_date": (yday - timedelta(days=i)).isoformat(), "stay_date": "2026-11-20", "source": "TO",
         "cancel_rn": 1, "cancel_rev": 150} for i in range(1, 8)]
cd = D.cancellation_driver(det, TODAY, 1000.0)
check("found", len(cd) == 1, str(cd))
if cd:
    c = cd[0]
    check("TO 15 of 17 rn", c["source"] == "TO" and c["source_rn"] == 15 and c["total_rn"] == 17)
    check("stay dates Nov 12–14", [d.day for d in c["top_dates"]] == [12, 13, 14], str(c["top_dates"]))
    check("contributions add up to the total", c["reconciles"])
    cand = _discovery_candidate(c, _signal_context({"report_date_iso": REPORT, "pace": []}))
    fb = cand["fallback_card"]
    check("headline", fb["headline"] == "Yesterday's cancellations: mostly TO, for Nov 12–Nov 14 stays", fb["headline"])
    template_ok(fb, "cancel_driver")
normal = [{"cancel_date": yday.isoformat(), "stay_date": "2026-11-12", "source": "TO", "cancel_rn": 1, "cancel_rev": 150},
          {"cancel_date": yday.isoformat(), "stay_date": "2026-12-02", "source": "Direct", "cancel_rn": 2, "cancel_rev": 300}]
check("normal day (1 TO rn, its usual rate) → nothing", not D.cancellation_driver(normal + det[4:], TODAY, 1000.0))
check("spread across sources (no 60% source) → nothing", not D.cancellation_driver(
    [dict(r, source=s) for r, s in zip(det[:4], ("TO", "BK", "OTA", "Direct"))], TODAY, 1000.0))
check("malformed rows are skipped", D.cancellation_driver([{"cancel_date": "bad"}] + det, TODAY, 1000.0) != [])

# ── T9: dark by default; live folds into the month's card ───────────────────
print("T9. dark launch + folding")
pace_dec = [{"month": "Dec", "month_num": 12, "rn": 868, "rn_stly": 1178, "rn_final_ly": 0, "rev": 130_200,
             "rev_stly": 176_700, "rev_final": 0, "adr": 150.0, "adr_stly": 150.0, "adr_final_ly": 150.0,
             "occ": 0.28, "stly": 0.38, "final": 0.5, "status": "behind"}]
data = {"report_date_iso": REPORT, "pace": pace_dec, "booking_cube": cube_t4, "mtd": {},
        "current_month_remaining": {}, "yesterday": {}}
out = _compute_signals(data)
check("discovery computed", len(out["discoveries"]) == 1, str([c["insight"]["id"] for c in out["discoveries"]]))
check("dark: never a candidate", not any(c["signal"] == "discovery" for c in out["ranked"] + out["watchlist"]))
config.ANTHROPIC_API_KEY = ""
res = A.generate_insights(data, hotel_id=None)
watch = [a for a in res["_meta"]["cards_audit"] if str(a.get("card_id", "")).startswith("discovery:")]
check("dark: recorded in cards_audit as WATCH", len(watch) == 1 and watch[0]["tag"] == "WATCH", str(watch))
check("dark: not counted as a fallback", res["_meta"]["fallback_cards"] == sum(
    1 for a in res["_meta"]["cards_audit"] if a.get("fallback_used")))
check("dark: no discovery in the owner's cards", not any(i["id"].startswith("disc_") for i in res["insights"]))
A._DISCOVERY_CARDS = True
try:
    out = _compute_signals(data)
    pace_c = [c for c in out["ranked"] + out["watchlist"] if c["signal"] == "pace"]
    check("live: no separate discovery card (folds into the Dec pace card)",
          not any(c["signal"] == "discovery" for c in out["ranked"] + out["watchlist"]))
    check("live: Dec pace card carries the driver", pace_c and "driver_slice" in pace_c[0]["insight"]["facts"],
          str(pace_c and list(pace_c[0]["insight"]["facts"])))
    check("live: driver is the leading explanation", pace_c and pace_c[0]["insight"]["cause_hypotheses"][0]["text"]
          .startswith("BK bookings made 31–60 days ahead"))
    out = _compute_signals(dict(data, pace=[]))
    dc = [c for c in out["ranked"] + out["watchlist"] if c["signal"] == "discovery"]
    check("live without a host card: competes on its own", len(dc) == 1)
    check("fingerprint carries the slice gap", dc and dc[0]["novelty"]["metric"] == dc[0]["disc_metric"])
finally:
    A._DISCOVERY_CARDS = False

# ── Opera: cube computed from the grain extract ─────────────────────────────
print("Opera cube from grain rows")
from db.adapters.opera_oracle.fetcher import Row, compute_pack
stay = date(2026, 11, 13)                       # a Friday
ly_stay = stay - timedelta(days=364)            # also a Friday
rows = [Row(stay, date(2026, 10, 1), None, "BK", "A", 1, 1, 100.0, 113.0, 0, 0, 0, 0),
        Row(ly_stay, date(2025, 10, 1), None, "BK", "A", 1, 1, 100.0, 113.0, 0, 0, 0, 0),
        # cancelled AFTER the same point last year → still counted then
        Row(ly_stay, date(2025, 10, 2), date(2025, 11, 5), "BK", "C", 0, 0, 0, 0, 1, 113.0, 0, 0),
        # cancelled yesterday for a future stay → cancel_detail
        Row(date(2026, 11, 20), date(2026, 9, 1), TODAY - timedelta(days=1), "TO", "C", 0, 0, 0, 0, 2, 226.0, 0, 0)]
pack = compute_pack(rows, TODAY)
cube = [r for r in pack["booking_cube"] if r["stay_date"] == stay]
check("cube keyed by the TY stay date", len(cube) == 1, str(pack["booking_cube"]))
if cube:
    check("TY 1 rn, LY 2 rn (cancelled-later restored)", cube[0]["rn_ty"] == 1 and cube[0]["rn_ly"] == 2, str(cube[0]))
    check("same weekday", stay.weekday() == ly_stay.weekday() == 4)
    check("no room category on Opera", cube[0]["room_cat"] is None)
check("cancel detail", any(r["source"] == "TO" and r["cancel_rn"] == 2 for r in pack["cancel_detail"]),
      str(pack["cancel_detail"]))

print("Opera Q20 next year by source + Q21 pickup by source (2026-10-30)")
rows2 = [Row(date(2027, 1, 10), date(2026, 10, 27), None, "BK", "A", 1, 1, 100.0, 113.0, 0, 0, 0, 0),   # next-year stay, booked yesterday
         Row(date(2026, 1, 10), date(2025, 10, 1), None, "BK", "A", 1, 1, 100.0, 113.0, 0, 0, 0, 0)]    # this year's Jan, booked before the cap
pack2 = compute_pack(rows2, TODAY)
sn = [r for r in pack2["sources_next"] if r["stay_month"] == 1 and r["source"] == "BK"]
check("next-year Jan BK: 1 rn TY vs 1 rn STLY", sn and sn[0]["rn_ty"] == 1 and sn[0]["rn_stly"] == 1, str(pack2["sources_next"]))
pb = [r for r in pack2["pickup_by_source"] if r["source"] == "BK"]
check("pickup by source keyed by book date + stay date", pb and pb[0]["ref_date"] == date(2026, 10, 27)
      and pb[0]["stay_date"] == date(2027, 1, 10) and pb[0]["net_rn"] == 1, str(pack2["pickup_by_source"]))
from db.adapters.assemble import assemble_snapshot
p2 = assemble_snapshot(dict(pack2, inventory=[]), {"hotel_name": "T", "total_rooms": 100}, today=TODAY)
check("assemble tags Q20 rows with next year", any(r["stay_year"] == 2027 for r in p2["sources_by_month"]),
      str(p2["sources_by_month"]))
check("assemble passes pickup_by_source with ISO dates", p2["pickup_by_source"]
      and p2["pickup_by_source"][0]["stay_date"] == "2027-01-10", str(p2["pickup_by_source"]))

# ── assemble / publish / contract ───────────────────────────────────────────
print("assemble, publish strip, contract")
from db.adapters.assemble import assemble_snapshot
payload = assemble_snapshot({"booking_cube": [{"stay_date": stay, "source": "BK", "room_cat": 12, "lead_bucket": "31-60",
                                               "rn_ty": 1, "rev_ty": 113, "rn_ly": 2, "rev_ly": 226}],
                             "cancel_detail": [{"cancel_date": "garbage"}]},
                            {"hotel_name": "T", "total_rooms": 100}, today=TODAY)
check("cube passes through with ISO dates", payload["booking_cube"][0]["stay_date"] == "2026-11-13"
      and payload["booking_cube"][0]["room_cat"] == "12", str(payload["booking_cube"]))
check("a malformed row is skipped, the rest and the snapshot intact",
      payload["cancel_detail"] == [] and len(payload["booking_cube"]) == 1 and "pace" in payload)
check("report_date_iso = yesterday", payload["report_date_iso"] == "2026-10-27")
from briefing.cloud_push import _ANALYST_ONLY_KEYS
check("publish strips the analyst-only inputs",
      set(_ANALYST_ONLY_KEYS) == {"booking_cube", "cancel_detail", "pickup_by_source"})   # Q21 added 2026-10-30
from db.contract import build_data_quality
dq = build_data_quality({"pace": [], "pickup_daily": [1], "otb_by_date": [1], "current_month_remaining": {"a": 1}})
check("cube absent never flips legacy_mode", dq["legacy_mode"] is False and "booking_cube" in dq["missing_fields"],
      str(dq["missing_fields"]))
check("rows_fetched counts the cube", "booking_cube" in dq["rows_fetched"])

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
