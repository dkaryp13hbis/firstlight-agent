"""
Hero paragraph — deterministic-layer tests (slots, driver logic, fallback,
validator). The Claude call itself is exercised in production. No DB, no API.

Run:  py -3.13 test_hero.py
"""

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

from briefing.analyst import (_build_hero_slots, _driver_hint, _hero_fallback,
                              _hero_violations, _bad_numbers)
import json

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


print("— Driver hints —")
check("rate-led up", _driver_hint(1.0, 8.0) == "rate-led")
check("occupancy-led up", _driver_hint(9.0, 2.0) == "occupancy-led")
check("both up", _driver_hint(5.0, 5.0) == "occupancy and rate both contributing")
check("mainly softer rates", _driver_hint(-1.0, -7.0) == "mainly softer rates")
check("mixed: rate up occ down", _driver_hint(-6.0, 8.0) == "rate up, occupancy down")
check("flat", _driver_hint(0.5, -0.8) == "occupancy and rate both broadly flat")
check("missing LY -> empty", _driver_hint(None, 5.0) == "")

print("— Slots —")
data = {
    "yesterday": {"revenue": 61400, "revenueLY": 57700, "roomNights": 132,
                  "roomNightsLY": 131, "adr": 465, "adrLY": 440,
                  "occupancy": 0.842, "occupancyLY": 0.839},
    "mtd": {"revenue": 1200000, "revenueLY": 1100000, "roomNights": 2800,
            "roomNightsLY": 2650, "adr": 429, "adrLY": 415,
            "occupancy": 0.81, "occupancyLY": 0.78, "month_name": "July"},
}
slots = _build_hero_slots(data)
y = slots["yesterday"]
check("yday revenue formatted", y["revenue"] == "€61,400", y["revenue"])
check("yday vs LY", y["vs_ly"] == "+6.4%", y["vs_ly"])
check("yday ADR vs LY", y["adr_vs_ly"] == "+5.7%", y["adr_vs_ly"])
check("yday driver is rate-led", y["driver"] == "rate-led", y["driver"])
check("mtd month label", slots["mtd"]["month"] == "July MTD", slots["mtd"].get("month"))
check("no None values in slots", all(v is not None for b in slots.values() for v in b.values()))

# Zero-LY payload must not divide by zero and must drop the vs fields
slots0 = _build_hero_slots({"yesterday": {"revenue": 100, "revenueLY": 0},
                            "mtd": {}})
check("zero LY -> vs_ly dropped", "vs_ly" not in slots0["yesterday"])

print("— Fallback —")
cards = [
    {"headline": "Aug pacing +49.8% ahead of same time last year",
     "tag": "OPPORTUNITY", "at_stake": {"value": "€451,284"}},
    {"headline": "Jul 2026 guests book 3 days earlier than last year",
     "tag": "OPPORTUNITY"},
]
fb = _hero_fallback(slots, cards)
check("fallback starts with Good morning", fb.startswith("Good morning."))
check("fallback has yesterday line", "€61,400" in fb and "+6.4%" in fb, fb)
check("fallback has MTD line", "July MTD" in fb, fb)
check("fallback previews top card", "+49.8%" in fb, fb)
check("fallback carries no at-stake figure", fb.count("€451,284") == 0, fb)
check("fallback under 110 words", len(fb.split()) <= 110, str(len(fb.split())))
check("fallback passes its own validator", _hero_violations(fb) == [],
      str(_hero_violations(fb)))

print("— Validator —")
check("rejects missing greeting", _hero_violations("Hello. All fine.") != [])
check("rejects imperative sentence start",
      any("imperative" in v for v in _hero_violations(
          "Good morning. Raise the August rates.")))
check("rejects exclamation", any("exclamation" in v for v in _hero_violations(
    "Good morning. Great day!")))
check("rejects over-length", any("words" in v for v in _hero_violations(
    "Good morning. " + "word " * 125)))
check("accepts clean paragraph", _hero_violations(
    "Good morning. Yesterday finished at €61,400, +6.4% vs last year, rate-led. "
    "July MTD stands at €1,200,000. August looks strong.") == [])

print("— Word-limit contract (clamp) —")
from briefing.analyst import _clamp_words, _enforce_caps, _WORD_CAPS, _HERO_WORD_CAP

check("clamp leaves short text untouched", _clamp_words("one two three", 5) == "one two three")
long_text = "word " * 30
check("clamp trims to cap with ellipsis",
      len(_clamp_words(long_text, 10).split()) == 10
      and _clamp_words(long_text, 10).endswith("…"))
over_card = {"id": "x", "headline": "w " * 20, "what_happened": "ok",
             "why_it_matters": "ok", "recommended_action": "ok", "by_when": "ok"}
clamped = _enforce_caps(dict(over_card))
check("enforce_caps trims over-cap field",
      len(clamped["headline"].split()) == _WORD_CAPS["headline"])
check("enforce_caps leaves ok fields alone", clamped["what_happened"] == "ok")
check("hero validator rejects at exactly cap+1",
      any("words" in v for v in _hero_violations(
          "Good morning. " + "w " * (_HERO_WORD_CAP - 1))))
check("hero fallback under hero cap", len(fb.split()) <= _HERO_WORD_CAP)

print("— Numeric validator on hero —")
hay = json.dumps(slots) + json.dumps(cards)
check("verbatim numbers pass",
      _bad_numbers({"h": "Yesterday €61,400 was +6.4% ahead"}, hay) == [])
check("invented numbers fail",
      _bad_numbers({"h": "Revenue was €99,999"}, hay) != [])

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
