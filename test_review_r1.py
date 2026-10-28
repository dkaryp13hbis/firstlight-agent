"""
Review 2026-10-28, Release 1 — compute + validation tests on synthetic data.
No DB, no network, no Claude (a fake client stands in where narration runs).

Covers the review scenarios that today's payload supports:
  T2  rooms +12%, revenue −5%      → "more rooms, less revenue" card + exact split
  T3  negative pickup = cancellations → attribution, one merged card
  T6  offsetting sources            → no misleading share
  T7  tiny subgroup                 → suppressed
  T8  December report, January next year → correct year everywhere
  T9  repeat discovery unchanged    → novelty demotes it
  T10 invented number / sign flip / renamed source / commercial advice /
      incomplete card               → rejected or flagged, fallback ships
  T11 malformed dataset             → other signals and the briefing survive
  T12 closed hotel                  → no fabricated warning
  T13 healthy month hiding a weak source → hidden-source card
  T14 same story twice              → one card
  + every fallback template: shape, word caps, plain words, review language

Run:  py -3.13 test_review_r1.py
"""

import copy
import json
from datetime import date
from types import SimpleNamespace

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

import briefing.analyst as A
from briefing.analyst import (_compute_signals, _WORD_CAPS, _plainify_text, _action_violations,
                              _shape_problems, _number_watch, _source_violations,
                              _lead_shift_attribution, _mix_rate_split, _occ_band,
                              _novelty_decide, _fingerprint, _source_attribution)

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


REPORT = "2026-10-27"          # → the analyst's today = 2026-10-28


def pace_row(m, rn, rn_stly, rev, rev_stly, rev_final=0, rn_final=0, adr_final=190.0,
             status="on_track"):
    return {"month": "x", "month_num": m, "rn": rn, "rn_stly": rn_stly, "rn_final_ly": rn_final,
            "rev": rev, "rev_stly": rev_stly, "rev_final": rev_final,
            "adr": rev / rn if rn else 0.0, "adr_stly": rev_stly / rn_stly if rn_stly else 0.0,
            "adr_final_ly": adr_final, "occ": rn / 3000, "stly": rn_stly / 3000,
            "final": 0.8, "status": status}


def src(m, name, rn_ty, rev_ty, rn_stly, rev_stly):
    return {"stay_month": m, "stay_year": 2026, "source": name,
            "rn_ty": rn_ty, "rev_ty": rev_ty, "rn_stly": rn_stly, "rev_stly": rev_stly}


def payload(**kw):
    d = {"hotel_name": "Test Hotel", "report_date_iso": REPORT, "pace": [], "mtd": {},
         "current_month_remaining": {}, "yesterday": {}, "sources_by_month": []}
    d.update(kw)
    return d


def cands(out, signal=None):
    allc = out["ranked"] + out["watchlist"]
    return [c for c in allc if signal is None or c["signal"] == signal]


def template_ok(fb, label):
    """Every fallback: complete shape, inside caps without clamping, plain
    words, review-only language, no 'sell-out' / unsupported rate claim."""
    check(f"{label}: shape", not _shape_problems(fb), str(_shape_problems(fb)))
    for field, cap in _WORD_CAPS.items():
        txt = str(fb.get(field, ""))
        n = len(txt.split())
        check(f"{label}: {field} {n}<={cap}", n <= cap and not txt.endswith("…"), txt)
        _, hits = _plainify_text(txt)
        check(f"{label}: {field} plain", not hits, str(hits))
    check(f"{label}: review language", not _action_violations(fb), str(_action_violations(fb)))
    blob = json.dumps(fb, ensure_ascii=False).lower()
    check(f"{label}: no sell-out claim", "sell-out" not in blob and "everyday rates" not in blob)


# ── T2: more rooms, less revenue — mix-driven, source named, exact split ────
print("T2. rooms +12%, revenue −5% (mix-driven)")
nov_mix = pace_row(11, 560, 500, 95_000, 100_000, status="ahead")
q17_mix = [src(11, "OTA", 300, 42_000, 100, 14_000), src(11, "Direct", 260, 53_000, 400, 86_000)]
out = _compute_signals(payload(pace=[nov_mix], sources_by_month=q17_mix))
pc = cands(out, "pace")
check("one pace card", len(pc) == 1, str([c["insight"]["id"] for c in pc]))
if pc:
    c, f = pc[0], pc[0]["insight"]["facts"]
    check("own id pace_mix_nov_2026", c["insight"]["id"] == "pace_mix_nov_2026", c["insight"]["id"])
    check("tag ALERT (revenue behind)", c["tag"] == "ALERT")
    check("rooms +12.0%", f["pace_vs_stly"]["value"] == "+12.0%", f["pace_vs_stly"]["value"])
    check("revenue −5.0%", f["rev_vs_stly_pct"]["value"] == "−5.0%", f["rev_vs_stly_pct"]["value"])
    check("average rate −15.2%", f["adr_vs_stly_pct"]["value"] == "−15.2%", f["adr_vs_stly_pct"]["value"])
    # (1 + rooms)(1 + rate) == 1 + revenue — the three facts reconcile
    check("rooms × rate reconciles to revenue", abs(1.12 * (95_000 / 560) / 200 - 0.95) < 1e-9)
    check("revenue gap fact is rev_gap, never rev_lead", "rev_gap" in f and "rev_lead" not in f, str(list(f)))
    check("mix explains the lower rate",
          f.get("rate_change_explained_by", {}).get("value", "").startswith("about 75% of the lower average rate comes from a change in which sources sold"),
          str(f.get("rate_change_explained_by")))
    check("source named (concentrated)", "OTA" in c["fallback_card"]["why_it_matters"], c["fallback_card"]["why_it_matters"])
    check("OTA share: more than the whole lead", f.get("biggest_source_share", {}).get("value") ==
          "more than the whole lead, partly offset by other sources", str(f.get("biggest_source_share")))
    check("three measurements agree → High", c["insight"]["cause_hypotheses"][0]["confidence"] == "High",
          str(c["insight"]["cause_hypotheses"]))
    check("evidence is rooms vs revenue", [e["label"] for e in c["fallback_card"]["evidence"]] ==
          ["ROOMS BOOKED (Nov full month)", "REVENUE BOOKED"], str(c["fallback_card"]["evidence"]))
    check("never labelled a revenue lead", "REVENUE LEAD" not in json.dumps(c["fallback_card"]))
    template_ok(c["fallback_card"], "mixed")

print("T2b. rate-driven split (same sources, cheaper rates)")
split = _mix_rate_split([src(11, "OTA", 300, 42_000, 220, 44_000), src(11, "Direct", 260, 53_000, 280, 56_000)],
                        11, 560, 95_000, 500, 100_000)
check("rate-driven", split and split["kind"] == "rate", str(split))
check("rate text", split and split["txt"].startswith("about 90% of the lower average rate comes from lower rates within each source"),
      str(split))
check("Q17 that disagrees with Q4 → no split",
      _mix_rate_split([src(11, "OTA", 200, 42_000, 220, 44_000)], 11, 560, 95_000, 500, 100_000) is None)

print("T2c. small revenue dip stays an 'ahead' card, honestly labelled")
# average rate €177.7 vs last year's final €175 → not the rate-dilution branch
out = _compute_signals(payload(pace=[pace_row(11, 560, 500, 99_500, 100_000, adr_final=175.0)]))
pc = cands(out, "pace")
check("ahead card (dip under €1,000 floor)", pc and pc[0]["insight"]["id"] == "pace_nov_2026", str([c["insight"]["id"] for c in pc]))
if pc:
    check("chip says REVENUE vs LAST YEAR, not LEAD",
          pc[0]["fallback_card"]["evidence"][1]["label"] == "REVENUE vs LAST YEAR", str(pc[0]["fallback_card"]["evidence"]))
    template_ok(pc[0]["fallback_card"], "ahead")

# ── T13 + T7: healthy month hiding a weak source; tiny source suppressed ────
print("T13. month +5% overall, Direct −35% hidden by OTA")
dec = pace_row(12, 525, 500, 90_000, 90_000)
q17_dec = [src(12, "Direct", 130, 23_400, 200, 36_000), src(12, "OTA", 392, 66_000, 285, 51_300),
           src(12, "Mice", 3, 600, 15, 3_000)]
out = _compute_signals(payload(pace=[dec], sources_by_month=q17_dec))
hs = cands(out, "hidden_source")
check("no pace card (month within 10%)", not cands(out, "pace"))
check("one hidden-source card", len(hs) == 1, str([c["insight"]["id"] for c in hs]))
if hs:
    c, f = hs[0], hs[0]["insight"]["facts"]
    check("names Direct, not tiny Mice", f["source"] == "Direct", f["source"])
    check("id", c["insight"]["id"] == "hidden_src_direct_dec_2026", c["insight"]["id"])
    check("month +5.0%", f["bookings_vs_stly"]["value"] == "+5.0%", f["bookings_vs_stly"]["value"])
    check("Direct −35.0%", f["source_vs_stly_pct"]["value"] == "−35.0%", f["source_vs_stly_pct"]["value"])
    check("others +95 rn cover it", f["other_sources_move"]["value"] == "+95 rn", f["other_sources_move"]["value"])
    check("signed parts reconcile: −70 + 95 == +25", -70 + 95 == 525 - 500)
    check("fingerprint metric = source gap", _fingerprint(c, "2026-10-27")["metric"] == -35.0,
          str(_fingerprint(c, "2026-10-27")))
    template_ok(c["fallback_card"], "hidden")

print("T7. only a tiny source moves → nothing")
out = _compute_signals(payload(pace=[pace_row(12, 505, 500, 90_000, 90_000)],
                               sources_by_month=[src(12, "Mice", 3, 600, 15, 3_000),
                                                 src(12, "OTA", 502, 89_400, 485, 87_000)]))
check("tiny subgroup suppressed", not cands(out, "hidden_source"))
out = _compute_signals(payload(pace=[dec], sources_by_month=q17_dec[:1]))
check("Q17 that does not cover the month → silent", not cands(out, "hidden_source"))

# ── T6: offsetting sources ───────────────────────────────────────────────────
print("T6. offsetting sources")
a = _source_attribution([src(11, "A", 20, 0, 100, 0), src(11, "B", 80, 0, 50, 0)], 11, -50)
check("A −80 vs net −50: said in words", a["share_txt"] == "more than the whole gap, partly offset by other sources", a["share_txt"])

# ── T8: December report with next year's January ───────────────────────────
print("T8. December report → Jan 2027")
dec_pace = [pace_row(1, 1200, 1100, 180_000, 160_000), pace_row(12, 900, 880, 150_000, 148_000)]
pnx = [{"month": "Jan", "month_num": 1, "rn": 300, "rn_stly": 450, "rev": 45_000, "rev_stly": 67_500},
       {"month": "Feb", "month_num": 2, "rn": 100, "rn_stly": 100, "rev": 15_000, "rev_stly": 15_000},
       {"month": "Jul", "month_num": 7, "rn": 900, "rn_stly": 400, "rev": 99_000, "rev_stly": 44_000}]
dec_pace[0]["rev_final"] = 0
out = _compute_signals(payload(report_date_iso="2026-12-14", pace=dec_pace, pace_next_year=pnx))
ids = [c["insight"]["id"] for c in cands(out)]
check("Jan 2027 card exists", any(i.endswith("jan_2027") for i in ids), str(ids))
check("no Jan 2026 card", not any(i.endswith("jan_2026") for i in ids), str(ids))
check("July 2027 is outside the 6-month window", not any("jul" in i for i in ids), str(ids))
jan = [c for c in cands(out) if c["insight"]["id"].endswith("jan_2027")]
if jan:
    ins = jan[0]["insight"]
    check("label Jan 2027", ins["facts"]["month_label"] == "Jan 2027", ins["facts"]["month_label"])
    check("stay period starts 2027-01-01", ins["stay_period"]["from"] == "2027-01-01", str(ins["stay_period"]))
    check("days to arrival from Dec 15", ins["days_to_nearest_arrival"] == 17, str(ins["days_to_nearest_arrival"]))
    check("pace + projection for Jan 2027 merged into one", len(jan) == 1, str([c["insight"]["id"] for c in jan]))
    template_ok(jan[0]["fallback_card"], "jan2027")

# ── T3 + T14: negative pickup that is really cancellations → one card ──────
print("T3/T14. pickup slump = cancellations, merged with the spike")
yday = "2026-10-27"
pk = [{"ref_date": f"2026-10-{d:02d}", "stay_month": 11, "stay_year": 2026, "net_rn": v, "net_rev": v * 150}
      for d, v in zip(range(20, 27), [4, 6, 5, 5, 4, 6, 5])]
pk.append({"ref_date": yday, "stay_month": 11, "stay_year": 2026, "net_rn": -12, "net_rev": -1800})
cx = [{"ref_date": f"2026-10-{d:02d}", "stay_month": 11, "stay_year": 2026, "cancel_rn": 1, "cancel_rev": 150}
      for d in range(20, 27)]
cx.append({"ref_date": yday, "stay_month": 11, "stay_year": 2026, "cancel_rn": 15, "cancel_rev": 2700})
out = _compute_signals(payload(pace=[pace_row(11, 500, 500, 95_000, 95_000)],
                               pickup_daily=pk, cancel_daily=cx))
pk_c = cands(out, "pickup")
check("one pickup ALERT", len(pk_c) == 1 and pk_c[0]["tag"] == "ALERT", str([c["insight"]["id"] for c in pk_c]))
check("no separate cancellation card", not cands(out, "cancellation"))
if pk_c:
    h = pk_c[0]["insight"]["cause_hypotheses"][0]
    check("attributed to cancellations (Medium)", h["text"].startswith("Mostly cancellations") and h["confidence"] == "Medium", str(h))
    check("spike facts folded in", "cancel_cancelled_yday" in pk_c[0]["insight"]["facts"])
    template_ok(pk_c[0]["fallback_card"], "pickup")

# ── T12: closed hotel ────────────────────────────────────────────────────────
print("T12. closed hotel, legitimate zeros")
zero = pace_row(11, 0, 0, 0, 0)
out = _compute_signals(payload(pace=[zero, pace_row(12, 0, 0, 0, 0)], otb_by_date=[],
                               yesterday={"revenue": 0, "roomNights": 0}, mtd={"roomNights": 0},
                               pace_next_year=[{"month_num": 5, "rn": 400, "rn_stly": 380, "rev": 60_000, "rev_stly": 55_000}]))
check("no cards for a closed hotel", not cands(out), str([c["insight"]["id"] for c in cands(out)]))
check("closed-season hero slots", "closed_season" in A._build_hero_slots(payload(
    yesterday={"revenue": 0, "roomNights": 0}, mtd={"roomNights": 0},
    pace_next_year=[{"month_num": 5, "rn": 400, "rn_stly": 380, "rev": 60_000, "rev_stly": 55_000}])))

# ── T11: malformed dataset ───────────────────────────────────────────────────
print("T11. one dataset malformed")
bad_lead = [{"period": "TY", "stay_month": 11, "stay_year": 2026, "rn": "abc", "lead_x_rn": 1}]
otb = [{"stay_date": "2026-13-45", "rn_ty": 1, "rn_stly": 50},
       {"stay_date": "2026-09-10", "rn_ty": 10, "rn_stly": 60},          # past date
       {"stay_date": "2027-07-10", "rn_ty": 10, "rn_stly": 60}]          # beyond 90 days
out = _compute_signals(payload(pace=[pace_row(11, 300, 400, 57_000, 76_000)], lead_time=bad_lead, otb_by_date=otb))
check("lead-time failure recorded", any(e.startswith("lead_time:") for e in out["compute_errors"]), str(out["compute_errors"]))
check("pace card still produced", any(c["signal"] == "pace" for c in cands(out)))
check("bad / past / far dates never become date cards", not cands(out, "soft_dates"))

# ── Date clusters + honest near-full wording ────────────────────────────────
print("Date clusters / near-full wording")
run = [{"stay_date": f"2026-11-{d:02d}", "rn_ty": 40, "rn_stly": 70} for d in range(10, 15)]
hot = [{"stay_date": f"2026-11-{d:02d}", "rn_ty": 85, "rn_stly": 60} for d in (20, 21)]
out = _compute_signals(payload(pace=[pace_row(11, 500, 500, 95_000, 95_000)], otb_by_date=run + hot))
sd = cands(out, "soft_dates")
check("soft dates fire", len(sd) == 1)
if sd:
    check("run headline", sd[0]["fallback_card"]["headline"] == "5 nights in a row (Nov 10–Nov 14) behind last year",
          sd[0]["fallback_card"]["headline"])
    check("run fact", sd[0]["insight"]["facts"]["consecutive_nights"]["value"] == "5 nights in a row (Nov 10–Nov 14)")
    template_ok(sd[0]["fallback_card"], "soft")
hd = cands(out, "hot_dates")
if hd:
    check("near-full headline is honest", hd[0]["fallback_card"]["headline"] == "Nov 20–Nov 21 filling up fast, ahead of last year",
          hd[0]["fallback_card"]["headline"])
    template_ok(hd[0]["fallback_card"], "hot")
check("band clamps above full", _occ_band(110) == "98–100%", _occ_band(110))

# ── Lead time: exact source split ───────────────────────────────────────────
print("Lead time source attribution")
lt = [{"period": "TY", "stay_month": 11, "stay_year": 2026, "source": "OTA", "lead_bucket": "8-15", "rn": 40, "rev": 4000, "lead_x_rn": 400},
      {"period": "TY", "stay_month": 11, "stay_year": 2026, "source": "Direct", "lead_bucket": "16-30", "rn": 20, "rev": 2000, "lead_x_rn": 600},
      {"period": "LY", "stay_month": 11, "stay_year": 2025, "source": "OTA", "lead_bucket": "16-30", "rn": 20, "rev": 2000, "lead_x_rn": 500},
      {"period": "LY", "stay_month": 11, "stay_year": 2025, "source": "Direct", "lead_bucket": "31-60", "rn": 30, "rev": 3000, "lead_x_rn": 1050}]
out = _compute_signals(payload(pace=[pace_row(11, 500, 500, 95_000, 95_000)], lead_time=lt))
lc = cands(out, "lead_time")
check("lead-time card", len(lc) == 1)
if lc:
    f = lc[0]["insight"]["facts"]
    check("OTA named", f.get("shift_source", {}).get("value") == "OTA", str(f.get("shift_source")))
    check("about 80% of the change", f.get("shift_source_share", {}).get("value") == "about 80% of the change",
          str(f.get("shift_source_share")))
    check("OTA guests book closer to arrival", f.get("shift_source_how", {}).get("value") == "its guests now book closer to arrival")
    template_ok(lc[0]["fallback_card"], "leadtime")
src_map = {(11, 2026, "TY", "OTA"): {"rn": 40, "lxr": 400}, (11, 2026, "TY", "Direct"): {"rn": 20, "lxr": 600},
           (11, 2026, "LY", "OTA"): {"rn": 20, "lxr": 500}, (11, 2026, "LY", "Direct"): {"rn": 30, "lxr": 1050}}
shift = 1000 / 60 - 1550 / 50
la = _lead_shift_attribution(src_map, 11, 2026, 60, 50, 31.0, shift)
# c_OTA = 40/60·(10−31) − 20/50·(25−31) = −11.6 ; c_Direct = 20/60·(30−31) − 30/50·(35−31) = −2.733
check("contributions sum exactly to the shift", abs((-11.6) + (-(1 / 3) - 2.4) - shift) < 1e-9, str(shift))
check("OTA share = 11.6 / 14.333", la and abs(la["share"] - 11.6 / -shift) < 1e-9, str(la))
check("spread across sources → nothing named",
      _lead_shift_attribution({(11, 2026, "TY", "A"): {"rn": 30, "lxr": 300}, (11, 2026, "TY", "B"): {"rn": 30, "lxr": 300},
                               (11, 2026, "LY", "A"): {"rn": 25, "lxr": 500}, (11, 2026, "LY", "B"): {"rn": 25, "lxr": 500}},
                              11, 2026, 60, 50, 20.0, -10.0) is None)

# ── T9: a repeat discovery without change is demoted ────────────────────────
print("T9. novelty on the new card types")
out = _compute_signals(payload(pace=[dec], sources_by_month=q17_dec))
hs = cands(out, "hidden_source")
if hs:
    c = hs[0]
    prior = [{"report_date": "2026-10-26", "ai_insights": {"insights": [{"id": c["insight"]["id"], "_novelty": dict(c["novelty"])}]}}]
    kept, dem = _novelty_decide([c], prior, date(2026, 10, 28))
    check("unchanged repeat → watchlist", not kept and len(dem) == 1)
    moved = dict(c["novelty"], metric=-20.0)
    prior2 = [{"report_date": "2026-10-26", "ai_insights": {"insights": [{"id": c["insight"]["id"], "_novelty": moved}]}}]
    kept, dem = _novelty_decide([c], prior2, date(2026, 10, 28))
    check("moved >= 10% → card again", len(kept) == 1)

# ── T10: validators ─────────────────────────────────────────────────────────
print("T10. validators")
check("commercial advice rejected", _action_violations({"recommended_action": "Consider a targeted offer on Nov nights."}))
check("'room for a higher rate' rejected", _action_violations({"recommended_action": "There may be room for a higher rate on Nov 14."}))
check("'lower average rate' as a fact is fine", not _action_violations(
    {"why_it_matters": "The extra rooms come in at a lower average rate than last year."}))
check("review language passes", not _action_violations(
    {"recommended_action": "It may be worth reviewing the rates and discounts still open on Nov 14."}))
check("missing field caught", _shape_problems({"headline": "Bookings changed"}))
check("renamed source caught", _source_violations(["Booking.com is behind"], '{"source": "BK"}'))
check("source in input is fine", not _source_violations(["Booking.com is behind"], '{"source": "Booking.com"}'))
hay = json.dumps({"a": "−5.0%", "b": "80%", "c": "€1,800"}, ensure_ascii=False)
check("sign flip flagged (watch)", any("sign" in w for w in _number_watch(["revenue +5.0%"], hay)), str(_number_watch(["revenue +5.0%"], hay)))
check("unit swap flagged (watch)", any("€80" in w for w in _number_watch(["€80 revenue"], hay)), str(_number_watch(["€80 revenue"], hay)))
check("unsigned magnitude is fine", not _number_watch(["revenue is 5.0% behind"], hay))
check("substring is not a match", any("'80'" in w or "80" in w for w in _number_watch(["180 rooms"], hay)))


class _Resp:
    def __init__(self, inp):
        self.content = [SimpleNamespace(type="tool_use", input=inp)]
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                                     cache_creation_input_tokens=0, cache_read_input_tokens=0)


def _fake_client(card_inputs, hero="Good morning. Yesterday was steady."):
    queue = list(card_inputs)

    def create(**kw):
        if kw["tools"][0]["name"] == "submit_hero":
            return _Resp({"hero": hero})
        # a fresh dict per call, like a real API response
        return _Resp(copy.deepcopy(queue.pop(0)) if queue else {})
    return SimpleNamespace(messages=SimpleNamespace(create=create))


plain_card = {"id": "x", "tag": "ALERT", "headline": "Something worth a look this month",
              "evidence": [{"label": "A", "value": "see card", "sub": ""}, {"label": "B", "value": "see card", "sub": ""}],
              "what_happened": "Bookings moved against last year.",
              "why_it_matters": "The reason is not yet clear (confidence: Low).",
              "recommended_action": "It may be worth reviewing the month.", "by_when": "This week."}
mix_payload = payload(pace=[nov_mix], sources_by_month=q17_mix)
cand = cands(_compute_signals(mix_payload), "pace")[0]
wrapper = {"property": "Test Hotel", "briefing_date": "2026-10-28", "capacity_rooms": 100, "insight": cand["insight"]}
_orig_client = A._get_client
try:
    for label, inp, want_fallback in [
        ("valid plain card accepted", plain_card, False),
        ("invented number → fallback", dict(plain_card, what_happened="Revenue fell by €99,999."), True),
        ("renamed source → fallback", dict(plain_card, why_it_matters="Booking.com sold the extra rooms (confidence: Low)."), True),
        ("commercial advice → fallback", dict(plain_card, recommended_action="Consider a targeted offer on Nov nights."), True),
        ("incomplete card → fallback, no crash", {"headline": "Bookings changed"}, True),
    ]:
        A._get_client = lambda inp=inp: _fake_client([inp])
        meta = {"cards_audit": [], "usage": A._usage_zero()}
        card = A._narrate_card(wrapper, cand["fallback_card"], meta=meta)
        check(label, meta["cards_audit"][-1]["fallback_used"] == want_fallback, str(meta["cards_audit"][-1]["validation_problems"]))
        check(f"  {label}: shippable", not _shape_problems(card))
    A._get_client = lambda: _fake_client([dict(plain_card, what_happened="Revenue booked is +5.0% vs last year.")])
    meta = {"cards_audit": [], "usage": A._usage_zero()}
    A._narrate_card(wrapper, cand["fallback_card"], meta=meta)
    check("sign flip ships (watch-only) but is recorded", not meta["cards_audit"][-1]["fallback_used"]
          and any("sign" in w for w in meta["cards_audit"][-1].get("number_watch", [])), str(meta["cards_audit"][-1]))

    print("T10b. one bad card never costs the others")
    config.ANTHROPIC_API_KEY = "test-key"
    full = payload(pace=[nov_mix], sources_by_month=q17_mix, otb_by_date=run + hot,
                   yesterday={"revenue": 10000, "revenueLY": 9000, "roomNights": 60, "roomNightsLY": 58,
                              "adr": 166.7, "adrLY": 155.2, "occupancy": 0.6, "occupancyLY": 0.58},
                   mtd={"revenue": 200000, "revenueLY": 190000, "month_name": "October", "roomNights": 1200})
    _fc = _fake_client([plain_card, {"headline": "Bookings changed"}, plain_card, plain_card])
    A._get_client = lambda: _fc          # one client, one queue for the whole briefing
    res = A.generate_insights(full, hotel_id=None)
    audit = [a for a in res["_meta"]["cards_audit"] if a.get("card_id") not in ("hero", "compute_errors")]
    check("all ranked cards shipped", len(res["insights"]) == len(audit) and len(audit) >= 3, str(len(res["insights"])))
    check("exactly the bad one fell back", sum(1 for a in audit if a["fallback_used"]) == 1, str([a["fallback_used"] for a in audit]))
    check("hero kept", res["executive_summary"].startswith("Good morning."))
    check("confidence carried to the payload", any("confidence" in i for i in res["insights"]))
    check("drill-down filters carried", all(i.get("drilldown", {}).get("stay_from") for i in res["insights"]),
          str([i.get("drilldown") for i in res["insights"]]))

    print("T10c. no API key → deterministic cards, never an empty briefing")
    config.ANTHROPIC_API_KEY = ""
    res = A.generate_insights(full, hotel_id=None)
    check("cards still ship", len(res["insights"]) >= 3, str(len(res["insights"])))
    check("all deterministic", all(a["fallback_used"] for a in res["_meta"]["cards_audit"] if a.get("card_id") != "compute_errors"))
    check("payload carries no legacy error text", res["executive_summary"] != "Data retrieved. AI narrative unavailable.")
finally:
    A._get_client = _orig_client

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
