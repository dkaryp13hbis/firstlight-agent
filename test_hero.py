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
check("rate-led up", _driver_hint(1.0, 8.0) == "mostly from higher rates")
check("occupancy-led up", _driver_hint(9.0, 2.0) == "mostly from more rooms sold")
check("both up", _driver_hint(5.0, 5.0) == "more rooms sold and higher rates")
check("mainly softer rates", _driver_hint(-1.0, -7.0) == "mainly lower rates")
check("mixed: rate up occ down", _driver_hint(-6.0, 8.0) == "higher rates, fewer rooms sold")
check("flat", _driver_hint(0.5, -0.8) == "rooms sold and rates both about the same")
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
check("yday driver is rate-led", y["driver"] == "mostly from higher rates", y["driver"])
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

print("— Plain-language pass —")
from briefing.analyst import _plainify_text, _plainify_card, _hero_fallback, _PLAIN_TERMS
_J = [
    ("Demand is firming.", "Demand is getting stronger."),
    ("Pace is decelerating.", "Bookings are slowing down."),
    ("Demand deterioration in Sep.", "Weaker demand in Sep."),
    ("Three compression dates ahead.", "Three dates filling up fast ahead."),
    ("ADR dilution is visible.", "Falling average rate is visible."),
    ("Pickup velocity weakened.", "New bookings have slowed."),
    ("The advantage is eroding.", "The gap is getting smaller."),
    ("Demand shifted earlier.", "Guests booked earlier than last year."),
    ("A soft-date cluster in Oct.", "A several dates with low bookings in Oct."),
    ("Underlying demand trend is flat.", "Recent booking trend is flat."),
]
for src, want in _J:
    got, _ = _plainify_text(src)
    check(f"plain: {src[:28]}", got == want, got)
got, hits = _plainify_text("Aug OTB revenue €61,400 pacing +12.5% vs STLY; ADR €465.")
check("plain: acronyms mid-sentence stay lowercase",
      got == "Aug booked so far revenue €61,400 running +12.5% vs same time last year; average rate €465.", got)
check("plain: numbers untouched", all(n in got for n in ("€61,400", "+12.5%", "€465")))
check("plain: reports what it replaced", set(hits) == {"OTB", "pacing", "STLY", "ADR"}, hits)
check("plain: clean text untouched", _plainify_text("Bookings are ahead of last year.")[0] == "Bookings are ahead of last year.")
card, hits = _plainify_card({"id": "x", "headline": "Sep pickup surging", "evidence": [{"label": "OTB"}],
                             "what_happened": "Net pickup +43 rn.", "why_it_matters": "", "recommended_action": "The position could support firmer rates.", "by_when": "Today"})
check("plain card: headline", card["headline"] == "Sep new bookings surging", card["headline"])
check("plain card: action opener", card["recommended_action"] == "There may be room for higher rates.", card["recommended_action"])
check("plain card: evidence labels untouched", card["evidence"][0]["label"] == "OTB")
_fb = _hero_fallback({"yesterday": {"revenue": "€61,400", "vs_ly": "+6.4%", "driver": "mostly from higher rates"},
                      "mtd": {"revenue": "€1,200,000", "month": "July MTD"}},
                     [{"headline": "Aug bookings +49.8% ahead of same time last year"}])
_fb_plain, _fb_hits = _plainify_text(_fb)
check("hero fallback carries no jargon", not _fb_hits, _fb_hits)
for _pat, _ in _PLAIN_TERMS:
    pass
check("plain terms compile and are ordered longest-first-ish", _PLAIN_TERMS[0][0].pattern.startswith("demand is firming"))

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
