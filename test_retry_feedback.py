"""
Surgical retry feedback (word-caps option 1) — deterministic tests.

Run:  py -3.13 test_retry_feedback.py
"""

import config
config.TOTAL_ROOMS = 100
config.HOTEL_NAME = "Test Hotel"

from briefing.analyst import _retry_feedback, _style_violations

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


card = {
    "headline": "August pace surging far ahead of last year on volume",
    "what_happened": ("Net pickup for Aug 2026 was +43 rn yesterday against a "
                      "7-day average of +21.0 rn/day which is well above the "
                      "recent trend"),
    "why_it_matters": "Short.",
    "recommended_action": "Raise the August rates on open nights now.",
    "by_when": "Today.",
}

style = _style_violations(card)
check("cap violation detected", any("what_happened is" in v for v in style), str(style))
check("imperative detected", any("imperative" in v for v in style), str(style))

fb = _retry_feedback(card, ["99,999"], style, ["period problem X"])

check("quotes the offending what_happened text",
      "Net pickup for Aug 2026 was +43 rn" in fb)
check("names the cap and a target",
      "the cap is 20" in fb and "target 18" in fb, fb[:400])
check("quotes the imperative action text",
      "Raise the August rates" in fb)
check("suggests soft openers", "Consider" in fb)
check("lists invented numbers", "99,999" in fb and "forbidden" in fb)
check("carries period violations through", "period problem X" in fb)
check("instructs unchanged fields stay verbatim",
      "resubmitted word-for-word unchanged" in fb)

# No cap match → message passes through untouched
fb2 = _retry_feedback(card, [], ["some unknown style problem"], [])
check("unknown style strings pass through", "some unknown style problem" in fb2)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
