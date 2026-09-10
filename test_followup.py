"""Follow-up engine pure-logic tests (plain script, no pytest).
Run: py -3.13 test_followup.py"""
from datetime import date

from briefing.followup import (
    month_key_from_card_id, month_gap, month_title, decide, closure_text,
    RECOVERY_GAP, RETIRE_DAYS,
)

P, F = 0, 0


def check(name, cond):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
        print(f"  FAIL: {name}")


D = date(2026, 9, 10)
PACE = {"pace": [
    {"month_num": 9, "rn": 400, "rn_stly": 380},   # +5.3%
    {"month_num": 10, "rn": 90, "rn_stly": 110},   # -18.2%
    {"month_num": 11, "rn": 20, "rn_stly": 0},     # no reference
]}

# ── card id → month key ─────────────────────────────────────────────────
check("pace_oct_2026", month_key_from_card_id("pace_oct_2026") == "2026-10")
check("pickup_sep_2026", month_key_from_card_id("pickup_sep_2026") == "2026-09")
check("no month suffix", month_key_from_card_id("cancel_spike") is None)
check("none id", month_key_from_card_id(None) is None)

# ── gap from pace ───────────────────────────────────────────────────────
g = month_gap(PACE, "2026-10")
check("oct gap ≈ -18.2", g is not None and abs(g + 18.18) < 0.1)
check("no stly → None", month_gap(PACE, "2026-11") is None)
check("missing month → None", month_gap(PACE, "2026-12") is None)
check("bad key → None", month_gap(PACE, "oops") is None)
check("title", month_title("2026-10") == "October")

# ── decide(): update / resolve / retire ─────────────────────────────────
row = {"flagged_date": "2026-09-08", "resolve_streak": 0, "last_gap_date": None}
a, p = decide(row, -12.0, D)
check("still behind → update streak 0", a == "update" and p["resolve_streak"] == 0)
check("last_gap recorded", p["last_gap"] == -12.0)

a, p = decide(row, -1.0, D)
check("first recovery day → update streak 1", a == "update" and p["resolve_streak"] == 1)

row2 = {"flagged_date": "2026-09-08", "resolve_streak": 1, "last_gap_date": "2026-09-09"}
a, _ = decide(row2, -1.5, D)
check("second recovery DAY → resolve", a == "resolve")

# same-day second run must not double-count the streak
row3 = {"flagged_date": "2026-09-08", "resolve_streak": 1, "last_gap_date": str(D)}
a, p = decide(row3, -1.5, D)
check("same-day rerun → still update", a == "update" and p["resolve_streak"] == 1)

# relapse resets the streak
row4 = {"flagged_date": "2026-09-08", "resolve_streak": 1, "last_gap_date": "2026-09-09"}
a, p = decide(row4, -9.0, D)
check("relapse → streak reset", a == "update" and p["resolve_streak"] == 0)

old = {"flagged_date": str(D.replace(month=8, day=25)), "resolve_streak": 0, "last_gap_date": None}
a, _ = decide(old, -9.0, D)
check("14+ days stuck → retire", a == "retire")

a, _ = decide(row, None, D)
check("no gap → skip", a == "skip")

# recovery threshold is the constant, not hard-coded
a, _ = decide(row, RECOVERY_GAP, D)
check("gap == threshold counts as recovering", a == "update")

# ── closure wording ─────────────────────────────────────────────────────
t = closure_text({"key": "2026-10", "first_gap": -18.2}, -1.0, "resolve")
check("resolve text", "October has recovered" in t and "1%" in t)
t = closure_text({"key": "2026-10", "first_gap": -18.2}, -15.0, "retire")
check("retire text", "still 15% behind" in t and "flag it again" in t)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
