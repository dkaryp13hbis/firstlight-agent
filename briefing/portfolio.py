"""
Multi-property portfolio — aggregates the LATEST stored briefing of every
hotel in a group into the shape the app's Portfolio view renders
(firstlight-pwa `fixtures/portfolio.ts`: PortfolioData / PHotel).

No new PMS fetch: everything derives from fields the snapshot already has.
  yd      yesterday block            ← data.yesterday
  mtd     month-to-date block        ← data.mtd
  ytd     year-to-date block         ← past months of data.pace (TY actuals,
                                        Final LY) + the mtd block
  months  12 × {TY OTB, STLY, Final LY, avail}   ← data.pace
  pickup  1/3/7-day windows          ← data.pickup;  14-day ← pickup_daily +
                                        cancel_daily (booked = net + cancelled)
  next    30 stay dates              ← data.otb_by_date (occupancy = rn / rooms)

Aggregation rules (spec frozen 2026-09-10): sums for revenue/nights,
occupancy = Σnights/Σavailable, ADR = Σrevenue/Σnights — the APP does the
sums; this module ships per-hotel blocks only. A hotel whose latest report
date is older than the group's newest one is flagged `stale` and the app
leaves it out of totals. Revenue is GROSS (the PMS figure the briefing
shows); `netFactor` = Σnet/Σgross of yesterday across the group so the
app's Net setting divides by the real ratio instead of the fixture's 1.13.
"""

import calendar
from datetime import date, datetime, timedelta
from typing import Any

_DOW_JS = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}   # python Mon=0 → JS Sun=0
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _f(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _short(name: str) -> str:
    """'City Hotel Thessaloniki' → 'City Hotel'; 'The Excelsior Hotel' → 'Excelsior'."""
    s = name
    for tail in (" Thessaloniki", " Hotel & Spa", " Wellness Spa Hotel"):
        if s.endswith(tail):
            s = s[: -len(tail)]
    if s.startswith("The "):
        s = s[4:]
    if s.endswith(" Hotel") and len(s) > 12:
        s = s[:-6]
    return s.strip() or name


def _open_months(season: dict | None, year: int) -> set[int] | None:
    """Months the hotel operates this year from season_settings; None = all year.
    Tolerates the known shapes: {"2026": {"open": "..", "close": ".."}} or
    {"open": "..", "close": ".."} or {"seasons": [{"year", "open", "close"}]}."""
    if not season:
        return None
    cand: list[tuple[date | None, date | None]] = []
    if isinstance(season, dict):
        y = season.get(str(year)) or season.get(year)
        if isinstance(y, dict):
            cand.append((_as_date(y.get("open") or y.get("opening")), _as_date(y.get("close") or y.get("closing"))))
        elif "open" in season or "opening" in season:
            cand.append((_as_date(season.get("open") or season.get("opening")), _as_date(season.get("close") or season.get("closing"))))
        for s in season.get("seasons") or []:
            if isinstance(s, dict) and str(s.get("year")) == str(year):
                cand.append((_as_date(s.get("open") or s.get("opening")), _as_date(s.get("close") or s.get("closing"))))
    months: set[int] = set()
    for o, c in cand:
        if not o or not c:
            continue
        for m in range(1, 13):
            first = date(year, m, 1)
            last = date(year, m, calendar.monthrange(year, m)[1])
            if o <= last and c >= first:
                months.add(m)
    return months or None


def build_hotel(hotel: dict, briefing: dict | None, ref_date: date) -> dict:
    """One PHotel. `briefing` = {report_date, data} (latest stored row) or None."""
    rooms = int(hotel.get("total_rooms") or 0)
    name = hotel.get("name") or "?"
    cfg = hotel.get("pms_config") or {}
    data = (briefing or {}).get("data") or {}
    rdate = _as_date((briefing or {}).get("report_date")) or ref_date
    stale = rdate.isoformat() if (not briefing or rdate < ref_date) else None
    today = rdate + timedelta(days=1)
    year = rdate.year
    open_m = _open_months(hotel.get("season_settings"), year)
    is_open = (lambda m: True) if open_m is None else (lambda m: m in open_m)

    # months from pace
    pace = {int(p.get("month_num") or 0): p for p in (data.get("pace") or []) if isinstance(p, dict)}
    months = []
    for m in range(1, 13):
        p = pace.get(m, {})
        days = calendar.monthrange(year, m)[1]
        months.append({
            "m": m, "open": bool(is_open(m)), "avail": rooms * days if is_open(m) else 0,
            "rnLY": _f(p.get("rn_final_ly")), "revLY": _f(p.get("rev_final")),
            "rnTY": _f(p.get("rn")), "revTY": _f(p.get("rev")),
            "rnST": _f(p.get("rn_stly")), "revST": _f(p.get("rev_stly")),
        })

    yd_d, mtd_d = data.get("yesterday") or {}, data.get("mtd") or {}
    yd = {"rnTY": _f(yd_d.get("roomNights")), "revTY": _f(yd_d.get("revenue")),
          "rnLY": _f(yd_d.get("roomNightsLY")), "revLY": _f(yd_d.get("revenueLY")),
          "avail": rooms if is_open(rdate.month) else 0}
    elapsed = rdate.day
    mtd = {"rnTY": _f(mtd_d.get("roomNights")), "revTY": _f(mtd_d.get("revenue")),
           "rnLY": _f(mtd_d.get("roomNightsLY")), "revLY": _f(mtd_d.get("revenueLY")),
           "avail": rooms * elapsed if is_open(rdate.month) else 0}
    past = [x for x in months if x["m"] < rdate.month]
    ytd = {"rnTY": sum(x["rnTY"] for x in past) + mtd["rnTY"],
           "revTY": sum(x["revTY"] for x in past) + mtd["revTY"],
           "rnLY": sum(x["rnLY"] for x in past) + mtd["rnLY"],
           "revLY": sum(x["revLY"] for x in past) + mtd["revLY"],
           "avail": sum(x["avail"] for x in past) + mtd["avail"]}

    # pickup windows: 1/3/7 from the card, 14 from the daily series
    pu = data.get("pickup") or {}
    pickup = {
        1: {"rn": _f((pu.get("last1d") or {}).get("roomNights")), "rev": _f((pu.get("last1d") or {}).get("revenue")),
            "cancel": _f(pu.get("cancellations1d")), "cancelRev": _f(pu.get("cancellationRevenue"))},
        3: {"rn": _f((pu.get("last3d") or {}).get("roomNights")), "rev": _f((pu.get("last3d") or {}).get("revenue")),
            "cancel": _f(pu.get("cancellations3d")), "cancelRev": _f(pu.get("cancellationRevenue3d"))},
        7: {"rn": _f((pu.get("last7d") or {}).get("roomNights")), "rev": _f((pu.get("last7d") or {}).get("revenue")),
            "cancel": _f(pu.get("cancellations7d")), "cancelRev": _f(pu.get("cancellationRevenue7d"))},
    }
    win14 = today - timedelta(days=13)
    net_rn = sum(_f(r.get("net_rn")) for r in data.get("pickup_daily") or []
                 if (_as_date(r.get("ref_date")) or win14) >= win14)
    net_rev = sum(_f(r.get("net_rev")) for r in data.get("pickup_daily") or []
                  if (_as_date(r.get("ref_date")) or win14) >= win14)
    cx_rn = sum(_f(r.get("cancel_rn")) for r in data.get("cancel_daily") or []
                if (_as_date(r.get("ref_date")) or win14) >= win14)
    cx_rev = sum(_f(r.get("cancel_rev")) for r in data.get("cancel_daily") or []
                 if (_as_date(r.get("ref_date")) or win14) >= win14)
    pickup[14] = {"rn": net_rn + cx_rn, "rev": net_rev + cx_rev, "cancel": cx_rn, "cancelRev": cx_rev}

    # next 30 stay dates from otb_by_date (starts today)
    otb = {_as_date(r.get("stay_date")): r for r in data.get("otb_by_date") or [] if isinstance(r, dict)}
    nxt = []
    for d in range(30):
        sd = today + timedelta(days=d)
        r = otb.get(sd) or {}
        av = rooms if is_open(sd.month) else 0
        nxt.append({"m": sd.month, "dom": sd.day, "dow": _DOW_JS[sd.weekday()],
                    "ty": (_f(r.get("rn_ty")) / rooms) if rooms and av else 0.0,
                    "st": (_f(r.get("rn_stly")) / rooms) if rooms and av else 0.0,
                    "avail": av})

    return {
        "id": str(hotel.get("id")), "name": name, "short": _short(name),
        "place": cfg.get("place") or "", "rooms": rooms,
        "stale": stale, "closedNow": not is_open(today.month), "reportDate": rdate.isoformat(),
        "months": months, "yd": yd, "mtd": mtd, "ytd": ytd, "pickup": pickup, "next": nxt,
        "netRatio": (_f(yd_d.get("revenueNet")) / _f(yd_d.get("revenue"))) if _f(yd_d.get("revenue")) else None,
    }


def build_portfolio(group_name: str, hotels: list[dict], briefings: dict[str, dict | None]) -> dict:
    """PortfolioData. `briefings` maps hotel id → latest {report_date, data}."""
    dates = [_as_date(b.get("report_date")) for b in briefings.values() if b and b.get("report_date")]
    ref = max(dates) if dates else date.today() - timedelta(days=1)
    items = [build_hotel(h, briefings.get(str(h.get("id"))), ref) for h in hotels]
    items.sort(key=lambda h: (h["stale"] is not None, -h["yd"]["revTY"]))
    gross = sum(_f((briefings.get(h["id"]) or {}).get("data", {}).get("yesterday", {}).get("revenue")) for h in items if not h["stale"])
    net = sum(_f((briefings.get(h["id"]) or {}).get("data", {}).get("yesterday", {}).get("revenueNet")) for h in items if not h["stale"])
    return {
        "groupName": group_name,
        "reportDate": ref.isoformat(),
        "reportLabel": f"{_WD[ref.weekday()]}, {_MON[ref.month - 1]} {ref.day}",
        "cur": ref.month, "elapsed": ref.day,
        "netFactor": (net / gross) if gross and net else None,
        "hotels": items,
    }
