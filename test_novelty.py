"""
No-repeat Pulse (2026-09-24): fingerprints, novelty decision, no 3-card floor,
hero pulse note. No DB, no Claude.

Run:  py -3.13 test_novelty.py
"""

import calendar
from datetime import date, timedelta

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

import briefing.analyst as A
from briefing.analyst import (_fingerprint, _novelty_decide, _compute_signals,
                              _hero_fallback, _pulse_note_parts, _HERO_WORD_CAP)

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
yday  = (today - timedelta(days=1)).isoformat()
d2    = (today - timedelta(days=2)).isoformat()
d3    = (today - timedelta(days=3)).isoformat()


def cand(cid, signal, facts=None, stake=0.0, days_out=45, novelty=None):
    c = {"signal": signal, "tag": "ALERT", "score": 0.5, "title_hint": cid,
         "stake_eur": stake,
         "insight": {"id": cid, "signal": signal, "facts": dict(facts or {}),
                     "days_to_nearest_arrival": days_out}}
    c["novelty"] = novelty if novelty is not None else _fingerprint(c, yday)
    return c


def prior(day, *insights):
    return {"report_date": day, "ai_insights": {"insights": list(insights)}}


def shipped(cid, novelty=None, stake=None):
    ins = {"id": cid}
    if novelty is not None:
        ins["_novelty"] = novelty
    if stake is not None:
        ins["_stake_eur"] = stake
    return ins


# ── A. fingerprints ──────────────────────────────────────────────────────────
print("A. fingerprints")
fp = _fingerprint(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−100 rn", "period": "x"}}, novelty={}), yday)
check("pace metric from rn_gap (typographic minus)", fp["metric"] == -100.0, str(fp))
fp = _fingerprint(cand("proj_oct_2026", "projection", {"vs_ref_band": {"value": "+8% to +13%", "period": "x"}}, novelty={}), yday)
check("projection metric from vs_ref_band", fp["metric"] == 8.0, str(fp))
fp = _fingerprint(cand("leadtime_oct_2026", "lead_time", {"lead_shift": {"value": "−12 days", "period": "x"}}, novelty={}), yday)
check("lead-time metric from lead_shift", fp["metric"] == -12.0, str(fp))
fp = _fingerprint(cand("hot_dates_near_full", "hot_dates", {"per_date": [{"date": "Oct 4"}, {"date": "Oct 3"}]}, novelty={}), yday)
check("hot dates key = sorted dates", fp["key"] == "date:Oct 3|date:Oct 4" and fp["metric"] is None, str(fp))
fp = _fingerprint(cand("soft_dates_oct", "soft_dates", {"per_date": [{"date": "Oct 9"}]}, stake=5000, novelty={}), yday)
check("soft dates key + stake metric", fp["key"] == "date:Oct 9" and fp["metric"] == 5000.0, str(fp))
fp = _fingerprint(cand("pickup_oct_2026", "pickup", stake=3000, novelty={}), yday)
check("pickup key = event day", fp["key"] == f"day:{yday}", str(fp))
fp = _fingerprint(cand("cancel_oct_2026", "cancellation", stake=3000, novelty={}), yday)
check("cancellation key = event day", fp["key"] == f"day:{yday}", str(fp))
fp = _fingerprint(cand("x", "unknown", stake=1234, novelty={}), yday)
check("fallback metric = stake", fp["metric"] == 1234.0, str(fp))

# ── B. decision rules ────────────────────────────────────────────────────────
print("B. decisions")
def decide(c, *rows):
    kept, dem = _novelty_decide([c], list(rows), today)
    return ("kept" if kept else "demoted"), c

r, _ = decide(cand("pace_nov_2026", "pace", {"rn_gap": {"value": "−100 rn", "period": "x"}}))
check("never seen → kept", r == "kept")

pace100 = {"rn_gap": {"value": "−100 rn", "period": "x"}}
r, c = decide(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−104 rn", "period": "x"}}, stake=15600),
              prior(d2, shipped("pace_oct_2026", {"metric": -100.0, "key": None, "days_out": 40}, 15000)))
check("pace moved 4% → demoted", r == "demoted")
check("  first_flagged annotated", "first_flagged" in c["insight"]["facts"], str(c["insight"]["facts"]))

r, c = decide(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−115 rn", "period": "x"}}, stake=17250),
              prior(d2, shipped("pace_oct_2026", {"metric": -100.0, "key": None, "days_out": 40}, 15000)))
check("pace worsened 15% → kept", r == "kept")
check("  since_first_flagged says wider", "wider" in c["insight"]["facts"].get("since_first_flagged", ""), str(c["insight"]["facts"]))

r, c = decide(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−85 rn", "period": "x"}}, stake=12750),
              prior(d2, shipped("pace_oct_2026", {"metric": -100.0, "key": None, "days_out": 40}, 15000)))
check("pace improved 15% → kept (news both ways)", r == "kept")
check("  since_first_flagged says narrower", "narrower" in c["insight"]["facts"].get("since_first_flagged", ""))

# last sighting wins: −100 → −104 → −112 is +7.7% vs LAST → demoted
r, _ = decide(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−112 rn", "period": "x"}}, stake=1),
              prior(d3, shipped("pace_oct_2026", {"metric": -100.0, "key": None, "days_out": 40}, 1)),
              prior(d2, shipped("pace_oct_2026", {"metric": -104.0, "key": None, "days_out": 40}, 1)))
check("metric compared against the LAST sighting", r == "demoted")

hot_prior = prior(d2, shipped("hot_dates_near_full", {"metric": None, "key": "date:Oct 3|date:Oct 4", "days_out": 9}))
r, _ = decide(cand("hot_dates_near_full", "hot_dates", {"per_date": [{"date": "Oct 4"}, {"date": "Oct 3"}]}, days_out=8), hot_prior)
check("hot dates, same dates → demoted", r == "demoted")
r, _ = decide(cand("hot_dates_near_full", "hot_dates", {"per_date": [{"date": "Oct 4"}, {"date": "Oct 11"}]}, days_out=8), hot_prior)
check("hot dates, a new date crossed → kept", r == "kept")
r, _ = decide(cand("hot_dates_near_full", "hot_dates", {"per_date": [{"date": "Oct 4"}]}, days_out=8), hot_prior)
check("hot dates, subset of old dates → demoted", r == "demoted")

r, _ = decide(cand("pickup_oct_2026", "pickup", stake=5000, days_out=10),
              prior(d2, shipped("pickup_oct_2026", {"metric": None, "key": f"day:{d2}", "days_out": 11}, 5000)))
check("pickup spike on a new day → kept", r == "kept")

r, c = decide(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−100 rn", "period": "x"}}, stake=15000, days_out=29),
              prior(d2, shipped("pace_oct_2026", {"metric": -100.0, "key": None, "days_out": 31}, 15000)))
check("unchanged but entered last 30 days → kept", r == "kept")
check("  status fact set", "30 days" in c["insight"]["facts"].get("status", ""))

# legacy prior payloads (no _novelty): old stake rule
r, _ = decide(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−105 rn", "period": "x"}}, stake=1050),
              prior(d2, shipped("pace_oct_2026", stake=1000)))
check("legacy prior, stake +5% → demoted", r == "demoted")
r, _ = decide(cand("pace_oct_2026", "pace", {"rn_gap": {"value": "−120 rn", "period": "x"}}, stake=1200),
              prior(d2, shipped("pace_oct_2026", stake=1000)))
check("legacy prior, stake +20% → kept", r == "kept")
r, _ = decide(cand("proj_oct_2026", "projection", {"vs_ref_band": {"value": "+8% to +13%", "period": "x"}}, stake=0),
              prior(d2, shipped("proj_oct_2026")))
check("legacy prior without stake, stake-less card → demoted (was: never demoted)", r == "demoted")
r, _ = decide(cand("hot_dates_near_full", "hot_dates", {"per_date": [{"date": "Oct 4"}]}, days_out=8),
              prior(d2, shipped("hot_dates_near_full")))
check("legacy prior without key, keyed card → demoted", r == "demoted")

# ── C. no floor in the compute path ──────────────────────────────────────────
print("C. no floor")
NM = today.month + 1 if today.month < 12 else today.month
pace = [{"month": calendar.month_abbr[NM], "month_num": NM, "rn": 300, "rn_stly": 400,
         "rn_final_ly": 600, "rev": 45_000, "rev_stly": 60_000, "rev_final": 90_000,
         "adr": 150.0, "adr_stly": 150.0, "adr_final_ly": 150.0, "occ": 0.1, "stly": 0.13, "final": 0.8}]
data = {"pace": pace, "mtd": {}, "current_month_remaining": {}}
out = _compute_signals(data)
check("candidates carry a fingerprint", all("novelty" in c for c in out["ranked"]) and out["ranked"], str(out["ranked"][:1]))
check("compute returns demoted list", "demoted" in out)
_orig = A._novelty_gate
A._novelty_gate = lambda cands, hid: ([], list(cands))      # every card is a repeat
try:
    out = _compute_signals(data, hotel_id="hotel-x")
    check("all repeats → 0 ranked cards (no backfill)", out["ranked"] == [], str([c["insight"]["id"] for c in out["ranked"]]))
    check("  repeats reported as demoted", len(out["demoted"]) >= 1 and out["watchlist"] == out["demoted"])
finally:
    A._novelty_gate = _orig

# ── D. hero pulse note ───────────────────────────────────────────────────────
print("D. hero pulse note")
slots = {"yesterday": {"revenue": "€12,840", "vs_ly": "−9.6%", "driver": "fewer rooms sold"},
         "mtd": {"month": "September", "revenue": "€198,400", "vs_ly": "+9.5%"}}
check("no pulse slot → no note", _pulse_note_parts(slots) == [])
slots["pulse"] = {"new_items": "0", "open_items": "3"}
note = _pulse_note_parts(slots)
check("quiet day note", note == ["Nothing new in the Pulse today; 3 items still open on the watchlist."], str(note))
slots["pulse"] = {"new_items": "1", "open_items": "1"}
check("singular forms", _pulse_note_parts(slots) == ["1 new item in the Pulse today; 1 item still open on the watchlist."], str(_pulse_note_parts(slots)))
slots["pulse"] = {"new_items": "2", "open_items": "0"}
check("no open items → new items only", _pulse_note_parts(slots) == ["2 new items in the Pulse today."])
slots["pulse"] = {"new_items": "0", "open_items": "3"}
hero = _hero_fallback(slots, [])
check("hero fallback ends with the note", hero.endswith("still open on the watchlist."), hero)
check("hero fallback starts with Good morning", hero.startswith("Good morning."))
check("hero fallback within cap", len(hero.split()) <= _HERO_WORD_CAP, str(len(hero.split())))
_, hits = A._plainify_text(hero)
check("hero fallback plain", not hits, str(hits))

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
