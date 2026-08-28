# Intraday notification gates + deterministic morning headline (plain script).
# Run: py -3.13 test_intraday.py
from datetime import date, timedelta

from briefing.intraday import check_intraday, headline

P, F = 0, 0
def check(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  ok  {name}")
    else: F += 1; print(f"FAIL  {name} {detail}")

today = str(date.today())
days = [str(date.today() - timedelta(days=i)) for i in range(9)]
cur_m = date.today().month

def snap(**kw):
    d = {
        "report_date": str(date.today() - timedelta(days=1)),
        "yesterday": {"revenue": 50000, "revenueLY": 48000},
        "mtd": {"revenue": 1000000, "revenueLY": 950000},
        "pickup": {"cancellationsToday": 0, "cancellations7d": 5,
                   "last7d": {"roomNights": 100}, "today": {"roomNights": 5}},
        "pace": [{"month": "Sep", "month_num": cur_m, "rev": 500000, "rev_stly": 480000, "rev_final": 900000}],
        "cancel_daily": [{"ref_date": days[i], "cancel_rn": 2} for i in range(1, 8)] +
                        [{"ref_date": today, "cancel_rn": 0}],
        "pickup_daily": [{"ref_date": days[i], "net_rn": 10} for i in range(1, 8)] +
                        [{"ref_date": today, "net_rn": 5}],
    }
    d.update(kw)
    return d

# ── headline ladder ──
from briefing.intraday import cancel_weeks
days14 = [str(date.today() - timedelta(days=i)) for i in range(14)]
def cancels(last7_per_day, prior7_per_day):
    return ([{"ref_date": days14[i], "cancel_rn": last7_per_day} for i in range(0, 7)] +
            [{"ref_date": days14[i], "cancel_rn": prior7_per_day} for i in range(7, 14)])

# prior-week window from Q14
l7, p7 = cancel_weeks(snap(pickup={"cancellations7d": 30}, cancel_daily=cancels(4, 2)))
check("cancel_weeks: last7 from pickup, prior7 from cancel_daily", (l7, p7) == (30, 14), (l7, p7))
check("cancel_weeks: no Q14 -> prior None", cancel_weeks(snap(cancel_daily=[]))[1] is None)

# rule: cancellations UP vs the week before (30 vs 14, churn 30%)
h = headline(snap(pickup={"cancellationsToday": 0, "cancellations7d": 30,
                          "last7d": {"roomNights": 100}, "today": {"roomNights": 0}},
                  cancel_daily=cancels(4, 2)))
check("headline: cancellations up vs prior week", h == "Cancellations up — 30 rooms out this week, vs 14 the week before.", h)

# the late-season trap: high churn ratio but NOT more than usual -> no warning
h = headline(snap(pickup={"cancellationsToday": 0, "cancellations7d": 20,
                          "last7d": {"roomNights": 100}, "today": {"roomNights": 0}},
                  cancel_daily=cancels(3, 3)))
check("headline: high churn but normal level -> not the cancellation rule", not h.startswith("Cancellations"), h)

# no Q14 -> cannot compare -> silent
h = headline(snap(pickup={"cancellationsToday": 0, "cancellations7d": 40,
                          "last7d": {"roomNights": 100}, "today": {"roomNights": 0}},
                  cancel_daily=[]))
check("headline: no prior-week data -> silent", not h.startswith("Cancellations"), h)

# capacity floor: 3% of a week at 236 rooms = 50 rn; 30 is below it
h = headline(snap(total_rooms=236,
                  pickup={"cancellationsToday": 0, "cancellations7d": 30,
                          "last7d": {"roomNights": 100}, "today": {"roomNights": 0}},
                  cancel_daily=cancels(4, 2)))
check("headline: below 3% weekly-capacity floor -> silent", not h.startswith("Cancellations"), h)

# a big yesterday now outranks cancellations
h = headline(snap(yesterday={"revenue": 80000, "revenueLY": 60000},
                  pickup={"cancellationsToday": 0, "cancellations7d": 30,
                          "last7d": {"roomNights": 100}, "today": {"roomNights": 0}},
                  cancel_daily=cancels(4, 2)))
check("headline: strong day outranks cancellations", h.startswith("Strong"), h)
check("headline: full euro number", "€80,000".replace(",", ".") in h, h)
h = headline(snap(pace=[{"month": "Oct", "month_num": cur_m, "rev": 400000, "rev_stly": 480000}]))
check("headline: month behind", h.startswith("Oct needs attention"), h)
h = headline(snap())
check("headline: steady fallback", h.startswith("Steady day"), h)

# ── intraday gates ──
prev = snap()
hits = check_intraday(snap(), prev)
check("quiet snapshot: no pushes", hits == [], str(hits))

# cancellation spike: 12 today vs ~2/day trailing
s = snap()
s["pickup"]["cancellationsToday"] = 12
s["cancel_daily"][-1] = {"ref_date": today, "cancel_rn": 12}
hits = check_intraday(s, prev)
check("cancel spike -> alert", any(h["type"] == "alerts" and "ancellations" in h["title"] for h in hits), str(hits))

# month slipped below -5 since morning
s = snap(pace=[{"month": "Sep", "month_num": cur_m, "rev": 450000, "rev_stly": 480000, "rev_final": 900000}])
hits = check_intraday(s, prev)
check("month slip -> alert", any(h["type"] == "alerts" and "Sep" in h["title"] for h in hits), str(hits))
hits2 = check_intraday(s, s)
check("no slip when prev already behind", not any(h["type"] == "alerts" for h in hits2), str(hits2))

# month crosses above LY final
s = snap(pace=[{"month": "Sep", "month_num": cur_m, "rev": 910000, "rev_stly": 480000, "rev_final": 900000}])
hits = check_intraday(s, prev)
check("passed final -> momentum", any(h["type"] == "momentum" and "beats last year" in h["title"] for h in hits), str(hits))
hits2 = check_intraday(s, s)
check("passed final only once", not any(h["type"] == "momentum" for h in hits2), str(hits2))

# strong booking day: 25 today vs 10/day trailing
s = snap()
s["pickup"]["today"]["roomNights"] = 25
s["pickup_daily"][-1] = {"ref_date": today, "net_rn": 25}
hits = check_intraday(s, prev)
check("strong day -> momentum", any(h["type"] == "momentum" and "Strong booking day" in h["title"] for h in hits), str(hits))

# caps: never more than one per type
s = snap(pace=[{"month": "Sep", "month_num": cur_m, "rev": 910000, "rev_stly": 480000, "rev_final": 900000},
               {"month": "Oct", "month_num": cur_m + 1, "rev": 200000, "rev_stly": 480000, "rev_final": 180000}])
s["pickup"]["cancellationsToday"] = 12
s["cancel_daily"][-1] = {"ref_date": today, "cancel_rn": 12}
hits = check_intraday(s, prev)
check("cap 1 alert", sum(1 for h in hits if h["type"] == "alerts") <= 1, str(hits))
check("cap 1 momentum", sum(1 for h in hits if h["type"] == "momentum") <= 1, str(hits))

# no euro figures in intraday texts (standing rule: only % and counts)
all_text = " ".join(h["title"] + h["body"] for h in hits)
check("no euro in intraday pushes", "€" not in all_text, all_text)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
