"""
Date-level demand engine — DARK by default (2026-10-31).

User (2026-10-30): "fl pulse insights are focused on just month? can we also
check daily? or weekly? sometimes there specific events that affects demand
for specific dates and not just the whole month" → "go" (hidden first).

Findings are recorded in refresh_runs.cards_audit ("dates:<id>", tag WATCH)
and reach an owner only with DATE_CARDS=1, after a few real mornings have
been reviewed. RAW NUMBERS ONLY — words, caps, scores and the fingerprint
live in briefing/analyst.py (`_date_candidate`).

Next 90 nights; every comparison is against the SAME WEEKDAY last year at the
same booking stage (Q18 booking cube, stay date − 364 d) — never Q10's
calendar dates, where a Saturday meets a Friday.

  surge     yesterday's net new bookings piled onto a few nights (Q21) — the
            earliest sign of an event, a group or a conference
  cluster   1–7 consecutive nights well ahead of / behind last year while the
            nights around them are not: a date story, not a month story
  ly_event  a 'behind' cluster whose LAST-YEAR nights were themselves far
            above the same weekdays around them, while this year's nights look
            normal: probably a one-off event last year, not a slowdown now
  week      a Mon–Sun week that stands apart from the rest of its month
  fill      1–7 consecutive nights booking far faster over the LAST 7 DAYS than
            the same weekdays around them, with rooms still free and a speed
            that would book them before the nights arrive (user 2026-10-31,
            PriceLabs' "fast-filling dates": "only when it happens")

Safety (discovery.py spirit — say nothing rather than mislead):
  * volume floors on every finding (last year's nights and the move itself);
  * a night with nothing booked this year is never 'behind' — closed or
    unknown (season settings are not in the payload on main; when they are,
    closed nights must be dropped here too);
  * runs longer than 7 nights are month stories, left to the month cards;
  * a cluster must stand out from the nights around it (>= 15 pts);
  * a source / segment is named only when it holds >= 60% of the move AND
    >= 20 pts more than its share of those nights ('size is not a cause');
    an unclassified '(none)' value is never named.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

HORIZON_DAYS   = 90
MIN_LY_RN      = 15      # cluster: last year's nights must carry real volume
MIN_MOVE_RN    = 10      # ...and the move must be at least this many room nights
MIN_DATE_PCT   = 0.15    # a night joins a cluster at >= 15% vs last year...
MIN_DATE_RN    = 2       # ...and >= 2 rn
MIN_DATE_LY    = 3       # a night with < 3 rn last year has no base to compare
MAX_RUN        = 7       # longer runs are month stories
CONTEXT_DAYS   = 14      # nights either side = the cluster's context
MIN_CONTEXT_LY = 30
MIN_STANDOUT   = 0.15    # cluster % gap vs context % gap
LY_EVENT_RATIO = 1.6     # last year's nights >= 60% above the same weekdays around them
TY_NORMAL_BAND = 0.25    # ...while this year's are within ±25% of theirs
SURGE_DATE_RN  = 3       # a night joins a surge with >= 3 rn net yesterday
SURGE_MIN_RN   = 10
SURGE_MIN_X    = 3.0     # >= 3× the normal daily net for those nights
SURGE_Z        = 2.0
WEEK_MIN_LY    = 30
WEEK_MIN_RN    = 15
WEEK_MIN_PCT   = 0.15
WEEK_STANDOUT  = 0.15
WEEK_OVERLAP   = 1       # a week holding ANY cluster night is the same story (a 2-night
                         # last-year spike made its whole week look weak — test 2026-10-31)
FILL_DAYS      = 7       # fill: net new bookings over the last 7 booking days (Q21)...
FILL_MIN_OUT   = 7       # ...for nights >= 7 days away: closer nights are the normal last-minute
                         # flow, and their same-weekday peers would all lie further out (slower)
FILL_PEERS     = 4       # usual speed = median of the 4 nearest same-weekday nights (1–4 weeks
FILL_MIN_PEERS = 2       # either side, inside the horizon, zero-filled), at least 2 of them
FILL_NIGHT_RN  = 3       # a night joins with >= 3 rn in 7 days...
FILL_NIGHT_ROOMS = 0.08  # ...and >= 8% of the hotel's rooms...
FILL_MIN_X     = 3.0     # ...and >= 3× the usual for its weekday
FILL_MIN_RN    = 8       # the run's 7-day total
FILL_YDAY_MAX  = 0.70    # mostly yesterday = a one-day jump, the surge's story
SHARE_MIN      = 0.60
SHARE_OVER     = 0.20
MAX_CLUSTERS   = 2
_UNCLASSIFIED  = ("(none)", "Unknown", "")


def _d(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _runs(dates: list[date]) -> list[list[date]]:
    """Consecutive-night runs from a sorted date list."""
    out: list[list[date]] = []
    for d in dates:
        if out and (d - out[-1][-1]).days == 1:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def daily_from_cube(cube: list[dict], today: date) -> dict[date, dict[str, float]]:
    """Per stay night: rooms + revenue booked now vs the same weekday last
    year at the same booking stage."""
    days: dict[date, dict[str, float]] = {}
    end = today + timedelta(days=HORIZON_DAYS)
    for r in cube or []:
        d = _d(r.get("stay_date"))
        if d is None or not today <= d <= end:
            continue
        x = days.setdefault(d, {"ty": 0.0, "ly": 0.0, "rev_ty": 0.0, "rev_ly": 0.0})
        x["ty"] += float(r.get("rn_ty") or 0)
        x["ly"] += float(r.get("rn_ly") or 0)
        x["rev_ty"] += float(r.get("rev_ty") or 0)
        x["rev_ly"] += float(r.get("rev_ly") or 0)
    return days


def _driver(moves: dict[str, float], volume: dict[str, float], sign: int,
            key: str) -> dict | None:
    """The one value of `key` behind a move — or None (safety-first)."""
    total, vol_total = sum(moves.values()), sum(volume.values())
    if not total or total * sign <= 0 or vol_total <= 0:
        return None
    name, delta = max(moves.items(), key=lambda kv: kv[1] * sign)
    if str(name).strip() in _UNCLASSIFIED or delta * sign < 5:
        return None
    share = delta / total
    weight = volume.get(name, 0.0) / vol_total
    if share >= SHARE_MIN and share - weight >= SHARE_OVER:
        return {"key": key, "name": str(name), "delta": delta, "share": share, "weight": weight}
    return None


def _cube_driver(cube: list[dict], nights: set[date], sign: int) -> dict | None:
    """Source first, then segment (Protel only — Opera's cube has no segment)."""
    for key in ("source", "segment"):
        moves: dict[str, float] = defaultdict(float)
        volume: dict[str, float] = defaultdict(float)
        seen_value = False
        for r in cube or []:
            if _d(r.get("stay_date")) not in nights:
                continue
            name = r.get(key)
            if name is None:
                continue
            seen_value = True
            ty, ly = float(r.get("rn_ty") or 0), float(r.get("rn_ly") or 0)
            moves[name] += ty - ly
            volume[name] += (ty + ly) / 2
        if seen_value:
            drv = _driver(moves, volume, sign, key)
            if drv:
                return drv
    return None


def _neighbour_ratio(daily: dict[date, dict], nights: list[date], field: str) -> float | None:
    """The cluster's nights against the mean of the SAME WEEKDAYS 1–2 weeks
    either side (open nights only). None when no neighbour exists."""
    num = den = 0.0
    for d in nights:
        nb = [daily[n][field] for n in (d - timedelta(days=14), d - timedelta(days=7),
                                        d + timedelta(days=7), d + timedelta(days=14))
              if n in daily and n not in nights and daily[n]["ty"] > 0]
        if not nb:
            return None
        num += daily[d][field]
        den += statistics.mean(nb)
    return num / den if den > 0 else None


def clusters(daily: dict[date, dict], cube: list[dict]) -> list[dict]:
    flags: dict[int, list[date]] = {1: [], -1: []}
    for d in sorted(daily):
        x = daily[d]
        if x["ty"] <= 0 or x["ly"] < MIN_DATE_LY:     # closed / unknown, or no base
            continue
        gap = x["ty"] - x["ly"]
        pct = gap / x["ly"]
        if pct >= MIN_DATE_PCT and gap >= MIN_DATE_RN:
            flags[1].append(d)
        elif pct <= -MIN_DATE_PCT and -gap >= MIN_DATE_RN:
            flags[-1].append(d)
    out: list[dict] = []
    for sign, dates in flags.items():
        for run in _runs(dates):
            if len(run) > MAX_RUN:
                continue                               # a month story
            ty = sum(daily[d]["ty"] for d in run)
            ly = sum(daily[d]["ly"] for d in run)
            gap = ty - ly
            if ly < MIN_LY_RN or abs(gap) < MIN_MOVE_RN:
                continue
            pct = gap / ly
            ctx = [d for d in daily if d not in run and daily[d]["ty"] > 0
                   and min(abs((d - run[0]).days), abs((d - run[-1]).days)) <= CONTEXT_DAYS]
            c_ty = sum(daily[d]["ty"] for d in ctx)
            c_ly = sum(daily[d]["ly"] for d in ctx)
            if c_ly < MIN_CONTEXT_LY:
                continue                               # nothing to stand out from
            c_pct = (c_ty - c_ly) / c_ly
            if (pct - c_pct) * sign < MIN_STANDOUT:
                continue
            ly_nb = _neighbour_ratio(daily, run, "ly")
            ty_nb = _neighbour_ratio(daily, run, "ty")
            kind = "cluster"
            if (sign < 0 and ly_nb is not None and ly_nb >= LY_EVENT_RATIO
                    and ty_nb is not None and abs(ty_nb - 1) <= TY_NORMAL_BAND):
                kind = "ly_event"
            rev_ly = sum(daily[d]["rev_ly"] for d in run)
            out.append({
                "type": kind, "sign": sign, "from": run[0], "to": run[-1],
                "dates": list(run), "ty": ty, "ly": ly, "gap": gap, "pct": pct,
                "ctx_pct": c_pct, "ly_vs_nb": ly_nb, "ty_vs_nb": ty_nb,
                "exposure_eur": abs(gap) * (rev_ly / ly if ly else 0.0),
                "driver": _cube_driver(cube, set(run), sign),
            })
    out.sort(key=lambda c: -abs(c["gap"]))
    return out[:MAX_CLUSTERS]


def weeks(daily: dict[date, dict], cube: list[dict], covered: set[date]) -> list[dict]:
    """The one Mon–Sun week that stands apart from the rest of its month
    (the month = the week's Thursday, ISO convention)."""
    by_week: dict[date, list[date]] = defaultdict(list)
    for d in daily:
        by_week[d - timedelta(days=d.weekday())].append(d)
    best: dict | None = None
    for monday, ds in by_week.items():
        if len(ds) != 7 or any(daily[d]["ty"] <= 0 for d in ds):
            continue                                   # partial or (partly) closed week
        if len(covered & set(ds)) >= WEEK_OVERLAP:
            continue                                   # a cluster already tells it
        thursday = monday + timedelta(days=3)
        ty = sum(daily[d]["ty"] for d in ds)
        ly = sum(daily[d]["ly"] for d in ds)
        if ly < WEEK_MIN_LY:
            continue
        gap = ty - ly
        pct = gap / ly
        rest = [d for d in daily if d not in ds and daily[d]["ty"] > 0
                and (d.year, d.month) == (thursday.year, thursday.month)]
        r_ty = sum(daily[d]["ty"] for d in rest)
        r_ly = sum(daily[d]["ly"] for d in rest)
        if r_ly < WEEK_MIN_LY:
            continue
        r_pct = (r_ty - r_ly) / r_ly
        sign = 1 if gap > 0 else -1
        if abs(gap) < WEEK_MIN_RN or abs(pct) < WEEK_MIN_PCT or (pct - r_pct) * sign < WEEK_STANDOUT:
            continue
        if best is None or abs(gap) > abs(best["gap"]):
            rev_ly = sum(daily[d]["rev_ly"] for d in ds)
            best = {
                "type": "week", "sign": sign, "from": monday, "to": monday + timedelta(days=6),
                "dates": sorted(ds), "ty": ty, "ly": ly, "gap": gap, "pct": pct,
                "rest_pct": r_pct, "month": thursday.month, "year": thursday.year,
                "exposure_eur": abs(gap) * (rev_ly / ly if ly else 0.0),
                "driver": _cube_driver(cube, set(ds), sign),
            }
    return [best] if best else []


def surge(pbs: list[dict], today: date) -> list[dict]:
    """Yesterday's net new bookings piled onto 1–7 consecutive nights, far
    above what those nights normally get in a day (Q21, net = bookings minus
    cancellations, Q9 conventions). Both directions; the largest one wins."""
    yday = today - timedelta(days=1)
    prior_days = [yday - timedelta(days=k) for k in range(1, 8)]
    y: dict[date, float] = defaultdict(float)
    y_rev: dict[date, float] = defaultdict(float)
    prior: dict[date, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    by_src: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    src_prior: dict[str, float] = defaultdict(float)
    for r in pbs or []:
        ref, sd = _d(r.get("ref_date")), _d(r.get("stay_date"))
        if ref is None or sd is None or sd < today:
            continue
        rn = float(r.get("net_rn") or 0)
        src = str(r.get("source") or "Direct")
        if ref == yday:
            y[sd] += rn
            y_rev[sd] += float(r.get("net_rev") or 0)
            by_src[sd][src] += rn
        elif ref in prior_days:
            prior[sd][ref] += rn
            src_prior[src] += abs(rn)
    total_yday = sum(y.values())
    best: dict | None = None
    for sign in (1, -1):
        nights = sorted(d for d, v in y.items() if v * sign >= SURGE_DATE_RN)
        for run in _runs(nights):
            if len(run) > MAX_RUN:
                continue
            new = sum(y[d] for d in run)
            series = [sum(prior[d][ref] for d in run) for ref in prior_days]   # zero-filled
            normal = statistics.mean(series)
            spread = max(statistics.pstdev(series), 1.0)
            z = (new - normal) / spread
            if new * sign < SURGE_MIN_RN or abs(new - normal) < SURGE_MIN_X * max(1.0, abs(normal)):
                continue
            if z * sign < SURGE_Z:
                continue
            moves: dict[str, float] = defaultdict(float)
            for d in run:
                for s, v in by_src[d].items():
                    moves[s] += v
            # a source's NORMAL share = its share of all net bookings in the
            # prior 7 days, every stay night (size is not a cause)
            vol = dict(src_prior) or {s: abs(v) for s, v in moves.items()}
            cand = {
                "type": "surge", "sign": sign, "from": run[0], "to": run[-1],
                "dates": list(run), "new_rn": new, "normal_per_day": normal, "z": z,
                "share_of_yday": (new / total_yday) if total_yday * sign > 0 and 0 < new / total_yday <= 1 else None,
                "exposure_eur": abs(sum(y_rev[d] for d in run)),
                "driver": _driver(moves, vol, sign, "source"),
            }
            if best is None or abs(new) > abs(best["new_rn"]):
                best = cand
    return [best] if best else []


def fill(pbs: list[dict], booked: dict[date, float], daily: dict[date, dict],
         rooms: float, today: date) -> list[dict]:
    """Fast-filling nights (Q21 net = bookings minus cancellations, Q9
    conventions). A night qualifies when its last-7-days net is >= 3 rn,
    >= 8% of the rooms and >= 3× the usual for its weekday (the median of the
    nearest same-weekday nights, zero-filled), and it still has rooms free.
    A run of 1–7 such nights is reported only when its 7-day total is >= 8 rn,
    the bookings came on several days (not mostly yesterday: that is a
    surge), and at this speed the free rooms would be booked before the first
    night arrives — the PriceLabs 'before they sell out' test, used as a gate
    only (never shown as a projection). The largest run wins."""
    if not rooms or rooms <= 0:
        return []
    yday = today - timedelta(days=1)
    first_ref = yday - timedelta(days=FILL_DAYS - 1)
    end = today + timedelta(days=HORIZON_DAYS)
    p7: dict[date, float] = defaultdict(float)
    p7_rev: dict[date, float] = defaultdict(float)
    p1: dict[date, float] = defaultdict(float)
    by_src: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    src_all: dict[str, float] = defaultdict(float)
    for r in pbs or []:
        ref, sd = _d(r.get("ref_date")), _d(r.get("stay_date"))
        if ref is None or sd is None or not first_ref <= ref <= yday or not today <= sd <= end:
            continue
        rn = float(r.get("net_rn") or 0)
        src = str(r.get("source") or "Direct")
        p7[sd] += rn
        p7_rev[sd] += float(r.get("net_rev") or 0)
        if ref == yday:
            p1[sd] += rn
        by_src[sd][src] += rn
        src_all[src] += abs(rn)

    def usual(d: date) -> float | None:
        peers: list[date] = []
        for k in range(1, 5):
            peers += [p for p in (d - timedelta(days=7 * k), d + timedelta(days=7 * k)) if today <= p <= end]
            if len(peers) >= FILL_PEERS:
                break
        if len(peers) < FILL_MIN_PEERS:
            return None
        return statistics.median(p7.get(p, 0.0) for p in peers[:FILL_PEERS])

    flagged: list[date] = []
    usual_of: dict[date, float] = {}
    for d in sorted(p7):
        if (d - today).days < FILL_MIN_OUT or d not in booked or rooms - booked[d] <= 0:
            continue                                   # too close, unknown, or sold out
        u = usual(d)
        v = p7[d]
        if u is None or v < FILL_NIGHT_RN or v < FILL_NIGHT_ROOMS * rooms or v < FILL_MIN_X * max(u, 1.0):
            continue
        flagged.append(d)
        usual_of[d] = u
    best: dict | None = None
    for run in _runs(flagged):
        if len(run) > MAX_RUN:
            continue                                   # a month story
        new = sum(p7[d] for d in run)
        if new < FILL_MIN_RN:
            continue
        yd = sum(p1[d] for d in run)
        if yd / new > FILL_YDAY_MAX:
            continue                                   # one day's jump: the surge tells it
        free = sum(rooms - booked[d] for d in run)
        days_out = (run[0] - today).days
        if new / FILL_DAYS * days_out < free:
            continue                                   # rooms would outlast the nights at this speed
        ty = sum(booked[d] for d in run)
        ly = sum(daily[d]["ly"] for d in run) if all(d in daily for d in run) else 0.0
        moves: dict[str, float] = defaultdict(float)
        for d in run:
            for s, v in by_src[d].items():
                moves[s] += v
        cand = {
            "type": "fill", "sign": 1, "from": run[0], "to": run[-1], "dates": list(run),
            "new_rn": new, "usual_rn": sum(usual_of[d] for d in run), "yday_rn": yd,
            "free": free, "capacity": rooms * len(run), "ty": ty,
            "ly": ly if ly >= MIN_DATE_LY * len(run) else None,     # no base → no comparison
            "exposure_eur": abs(sum(p7_rev[d] for d in run)),       # real revenue, internal ranking
            "driver": _driver(moves, dict(src_all) or {s: abs(v) for s, v in moves.items()}, 1, "source"),
        }
        if best is None or new > best["new_rn"]:
            best = cand
    return [best] if best else []


def discover_dates(data: dict, today: date) -> tuple[list[dict], list[str]]:
    """All date findings for one snapshot + non-fatal errors. Each part is
    fail-open: one broken piece never takes the others (or the briefing).
    One story per night: a fast-filling run replaces an overlapping 'ahead'
    cluster or upward surge (it carries yesterday's part and the same-weekday
    comparison itself), and a week holding a fill night is not told twice."""
    errors: list[str] = []
    cube = [r for r in (data.get("booking_cube") or []) if isinstance(r, dict)]
    pbs = [r for r in (data.get("pickup_by_source") or []) if isinstance(r, dict)]
    daily = daily_from_cube(cube, today) if cube else {}
    cl: list[dict] = []
    fl: list[dict] = []
    wk: list[dict] = []
    su: list[dict] = []
    if daily:
        try:
            cl = clusters(daily, cube)
        except Exception as exc:   # noqa: BLE001
            errors.append(f"date clusters: {type(exc).__name__}: {exc}"[:200])
    try:
        # rooms booked per night: the cube (same as Q10's this-year count); Q10 when no cube
        booked = ({d: x["ty"] for d, x in daily.items()} if daily else
                  {d: float(r.get("rn_ty") or 0) for r in (data.get("otb_by_date") or [])
                   if isinstance(r, dict) and (d := _d(r.get("stay_date"))) is not None})
        fl = fill(pbs, booked, daily, float(data.get("total_rooms") or 0), today)
    except Exception as exc:   # noqa: BLE001
        errors.append(f"date fill: {type(exc).__name__}: {exc}"[:200])
    filled = {d for f in fl for d in f["dates"]}
    cl = [c for c in cl if not (c["sign"] > 0 and filled & set(c["dates"]))]
    if daily:
        try:
            wk = weeks(daily, cube, {d for c in cl for d in c["dates"]} | filled)
        except Exception as exc:   # noqa: BLE001
            errors.append(f"date weeks: {type(exc).__name__}: {exc}"[:200])
    try:
        su = [s for s in surge(pbs, today) if not (s["sign"] > 0 and filled & set(s["dates"]))]
    except Exception as exc:   # noqa: BLE001
        errors.append(f"date surge: {type(exc).__name__}: {exc}"[:200])
    return cl + wk + su + fl, errors
