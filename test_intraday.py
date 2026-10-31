# Daytime notifications (FL Pulse gates on today-so-far) + morning push text + sender rules (plain script).
# Run: py -3.13 test_intraday.py
from datetime import date, timedelta

from briefing.intraday import headline

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

# ── daytime events = FL Pulse's own gates on today-so-far (2026-10-31) ──────
import os, sys, types
from datetime import datetime, timezone
import config
config.TOTAL_ROOMS = 50
from briefing import intraday as I
from briefing import cloud_push as CP
from briefing.analyst import _plainify_text

T = date(2026, 11, 2)                                   # "today" for the checks
def d(k):                                               # T - k days, ISO
    return (T - timedelta(days=k)).isoformat()

def day_data(pick_today=None, pick_prior=(2, 3, 4, 3, 2, 3, 4), cx_today=None,
             cx_prior=(0, 1, 2, 1, 0, 1, 1), cx_rev_today=1800, yday_spike=False):
    """Dec 2026 bookings + Jan 2027 cancellations; index 0 of the priors = T-7."""
    pd, cd = [], []
    for i, rn in enumerate(pick_prior):
        pd.append({"ref_date": d(7 - i), "stay_year": 2026, "stay_month": 12, "net_rn": rn, "net_rev": rn * 150})
    if pick_today is not None:
        pd.append({"ref_date": d(0), "stay_year": 2026, "stay_month": 12,
                   "net_rn": pick_today, "net_rev": pick_today * 150})
    for i, c in enumerate(cx_prior):
        c = 9 if (yday_spike and i == 6) else c
        cd.append({"ref_date": d(7 - i), "stay_year": 2027, "stay_month": 1, "cancel_rn": c,
                   "cancel_rev": c * 200})
    if cx_today is not None:
        cd.append({"ref_date": d(0), "stay_year": 2027, "stay_month": 1, "cancel_rn": cx_today,
                   "cancel_rev": cx_rev_today})
    return {"report_date_iso": d(1), "pace": [], "pace_next_year": [], "pickup_daily": pd, "cancel_daily": cd}

hits = I.pulse_checks(day_data(pick_today=20, cx_today=9), today=T)
by = {h["type"]: h for h in hits}
check("surge today -> one good-news event (Pulse signal 1)",
      by.get("momentum", {}).get("card_id") == "pickup_dec_2026", str(hits))
check("spike today -> one alert (Pulse signal 6)", by.get("alerts", {}).get("card_id") == "cancel_jan_2027", str(hits))
check("good-news text, plain words, real numbers",
      by.get("momentum", {}).get("body") == "+20 room nights added so far today for Dec 2026 (+€3,000), against a normal +3.0 a day.",
      by.get("momentum", {}).get("body"))
check("alert text, plain words, real numbers",
      by.get("alerts", {}).get("body") == "9 room nights cancelled so far today for Jan 2027 (€1,800 of room revenue), against a normal 0.9 a day.",
      by.get("alerts", {}).get("body"))
check("titles name the month", by.get("momentum", {}).get("title") == "Strong booking day for Dec 2026"
      and by.get("alerts", {}).get("title") == "Cancellations spiking for Jan 2027", str(hits))
txt = " ".join(h["title"] + " " + h["body"] for h in hits)
check("no jargon (rn, OTB, pickup) in push text", " rn" not in txt and "OTB" not in txt and not _plainify_text(txt)[1], txt)
check("at most one per type", len(hits) == len({h["type"] for h in hits}) <= 2)

check("a slow morning is never news (negative bookings z ignored)",
      not I.pulse_checks(day_data(pick_today=0, pick_prior=(10, 11, 9, 10, 12, 10, 9)), today=T))
check("an ordinary day says nothing", not I.pulse_checks(day_data(pick_today=4, cx_today=1), today=T))
check("2 cancellations are noise (Pulse floor 3 rn)", not I.pulse_checks(day_data(cx_today=2, cx_rev_today=900), today=T))
check("under €1,000 of cancelled revenue says nothing (Pulse stake floor)",
      not I.pulse_checks(day_data(cx_today=9, cx_rev_today=600), today=T))
check("yesterday's spike is the morning Pulse's story, not a daytime alert",
      not I.pulse_checks(day_data(cx_today=1, cx_rev_today=200, yday_spike=True), today=T))
check("missing series: silent, no crash", I.pulse_checks({"report_date_iso": d(1)}, today=T) == [])

# claim once per type per day; events carry the time; app item + push after publish
_claims = []
_orig_claim, _pc = I._claim, I.pulse_checks
I._claim = lambda hid, t: (_claims.append(t) or True)
I.pulse_checks = lambda data: _pc(data, today=T)
ev = I.intraday_events(day_data(pick_today=20, cx_today=9), "h1")
check("claimed events carry type, card id, month, text and HH:MM",
      {e["type"] for e in ev} == {"alerts", "momentum"} and all(len(e["at"]) == 5 for e in ev), str(ev))
I._claim = lambda hid, t: False
check("already sent today -> no event", I.intraday_events(day_data(pick_today=20, cx_today=9), "h1") == [])
I._claim, I.pulse_checks = _orig_claim, _pc

# ── morning push = FL Pulse's top headline ──
ai5 = {"insights": [{"headline": "Nov 11–14 bookings are half last year's level"}] + [{"headline": "x"}] * 4}
check("morning: count + top Pulse headline",
      CP.morning_body(ai5, None) == "5 insights in FL Pulse · Nov 11–14 bookings are half last year's level",
      CP.morning_body(ai5, None))
check("morning: singular", CP.morning_body({"insights": [{"headline": "A"}]}, None) == "1 insight in FL Pulse · A")
qb = CP.morning_body({"insights": []}, snap())
check("morning: quiet day -> rule ladder", qb.startswith("Nothing new in FL Pulse today · Steady day"), qb)
check("morning: test / no data -> the given text",
      CP.morning_body({"insights": [], "executive_summary": "Hello"}, None) == "Hello")
check("morning: <= 180 chars", len(CP.morning_body({"insights": [{"headline": "y" * 400}]}, None)) <= 180)

# ── quiet hours (Athens) ──
U = timezone.utc
check("summer 19:00 UTC = 22:00 Athens -> quiet", CP.in_quiet_hours(datetime(2026, 7, 1, 19, 0, tzinfo=U)))
check("winter 19:00 UTC = 21:00 Athens -> not quiet", not CP.in_quiet_hours(datetime(2026, 11, 2, 19, 0, tzinfo=U)))
check("winter 05:30 UTC = 07:30 Athens -> quiet", CP.in_quiet_hours(datetime(2026, 11, 2, 5, 30, tzinfo=U)))
check("winter 07:00 UTC = 09:00 Athens -> not quiet", not CP.in_quiet_hours(datetime(2026, 11, 2, 7, 0, tzinfo=U)))

# ── sender: one push per device, per-type choices, own slot per type, gone devices removed ──
sent, pruned = [], []
class _Resp:
    def __init__(self, code): self.status_code = code
class WebPushException(Exception):
    def __init__(self, msg, response=None): super().__init__(msg); self.response = response
def _webpush(subscription_info, data, vapid_private_key, vapid_claims):
    if subscription_info["endpoint"] == "gone":
        raise WebPushException("gone", response=_Resp(410))
    sent.append((subscription_info["endpoint"], data))
sys.modules["pywebpush"] = types.SimpleNamespace(webpush=_webpush, WebPushException=WebPushException)
os.environ["VAPID_PRIVATE_KEY"] = "test-key-not-a-file"
from db import store as _st
_st.push_delete_endpoint = lambda ep: pruned.append(ep)
rows = [{"subscription": {"endpoint": "phone-a"}, "notification_prefs": {}},
        {"subscription": {"endpoint": "phone-a"}, "notification_prefs": {}},          # same device twice
        {"subscription": {"endpoint": "tablet-a"}, "notification_prefs": {"alerts": True}},
        {"subscription": {"endpoint": "phone-b"}, "notification_prefs": {"alerts": False}},
        {"subscription": {"endpoint": "gone"}, "notification_prefs": {}}]
CP._subscriptions = lambda hid: rows
_quiet = CP.in_quiet_hours
CP.in_quiet_hours = lambda now=None, tz="Europe/Athens": False
CP.send_typed_push("h1", "Hotel", "T", "B", "alerts")
eps = [e for e, _ in sent]
check("every device once (duplicate row ignored)", sorted(eps) == ["phone-a", "tablet-a"], str(eps))
check("a person's 'alerts off' is respected", "phone-b" not in eps)
check("gone device removed (410)", pruned == ["gone"], str(pruned))
import json as _json
p = _json.loads(sent[0][1]) if sent else {}
check("own slot per type: tag firstlight-<hotel>-alerts", p.get("tag") == "firstlight-h1-alerts" and p.get("type") == "alerts", str(p))
sent.clear()
CP.send_typed_push("h1", "Hotel", "Test notification", "B", "test")
check("test push ignores the type choices", "phone-b" in [e for e, _ in sent], str(sent))
sent.clear()
CP.in_quiet_hours = lambda now=None, tz="Europe/Athens": True
CP.send_typed_push("h1", "Hotel", "T", "B", "momentum")
check("quiet hours hold good news", sent == [])
CP.send_typed_push("h1", "Hotel", "T", "B", "morning")
check("quiet hours never hold the morning briefing", len(sent) >= 1, str(sent))
CP.in_quiet_hours = _quiet

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
