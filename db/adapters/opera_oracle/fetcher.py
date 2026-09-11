"""
Opera 5 / Oracle adapter — fetch implementation.

fetch_snapshot(conn, hotel_ctx) pulls one bounded extract per property
(queries.Q_GRAIN) and computes the SAME result rows the Protel query pack
produces (Q1–Q16, identical column names and conventions), then hands them
to the shared assembly (`db/adapters/assemble.py`).

    hotel_ctx = {
        "hotel_name":   "City Hotel",
        "total_rooms":  125,
        "pms_hotel_id": "CITY",      # Opera RESORT code
        "hotel_type":   "city",
    }

Every window / cancellation / STLY rule below is a line-by-line port of
`protel_mssql/queries.py` — read that file's comments for the WHY. Where
Opera differs (per-night status, cancelled nights and lost revenue in their
own columns, gross = net + tax) the mapping is documented inline.

Night-audit gate: Opera posts room revenue at night audit. If the night we
report on is not CLOSED yet, fetch raises — the run fails at the data level
and the retry ladder (5/15/45 min) picks it up once the audit closes.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from db.adapters.opera_oracle import queries as Q
from db.adapters.assemble import assemble_snapshot

CONNECTOR = "oracle"

ACTIVE = ("A", "D")          # occupied night / departure-day row
CANCELLED = "C"

_LEAD_BUCKETS = ((3, "0-3"), (7, "4-7"), (15, "8-15"), (30, "16-30"),
                 (60, "31-60"), (90, "61-90"))

# Every column the Protel SELECT lists exists on every row — mirror that.
_PACE_KEYS = ("rn_otb_ty", "rn_stly", "rn_final_ly", "rev_otb_ty", "rev_stly",
              "rev_final_ly", "rev_otb_ty_net", "rev_stly_net", "rev_final_ly_net")
_PACE_NEXT_KEYS = ("rn_otb", "rn_stly", "rev_otb", "rev_stly", "rn_stly2", "rev_stly2")
_SOURCE_KEYS = ("rev_ty", "rn_ty", "rev_stly", "rn_stly")


def _full(acc: dict, keys: tuple, **fixed) -> dict:
    row = dict(fixed)
    for k in keys:
        row[k] = float(acc.get(k, 0.0))
    return row


# ------------------------------------------------------------------
# Grain rows
# ------------------------------------------------------------------

@dataclass(slots=True)
class Row:
    sd: date                 # stay night
    bd: date | None          # book date
    cd: date | None          # cancel date
    src: str
    sg: str                  # 'A' active night, 'D' departure-day row, 'C' cancelled
    has_rev: int
    rn: float
    net: float
    gross: float
    crn: float               # cancelled room nights
    cgross: float            # lost gross revenue of cancelled nights
    arr: float
    dep: float


def _d(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v


def _year_ago(d: date, years: int = 1) -> date:
    """SQL Server DATEADD(YEAR, -n, d): Feb 29 → Feb 28."""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def _year_ahead(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def _lead_bucket(days: int) -> str:
    for cap, label in _LEAD_BUCKETS:
        if days <= cap:
            return label
    return "90+"


def _eom(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


# ------------------------------------------------------------------
# Row measures (the Protel "restore cancelled" conventions)
# ------------------------------------------------------------------

def _alive_at(r: Row, cap: date) -> bool:
    """Protel: reschar < 2 OR (reschar = 2 AND Canceled > cap)."""
    return r.sg in ACTIVE or (r.sg == CANCELLED and r.cd is not None and r.cd > cap)


def _rn_of(r: Row) -> float:
    """Protel: CASE WHEN reschar < 2 THEN Occupancy ELSE 1-per-night END."""
    return r.rn if r.sg in ACTIVE else r.crn


def _rev_of(r: Row) -> float:
    """Protel logis (gross lodging) — kept on cancelled records; Opera keeps
    it in CLX_ROOM_REVENUE_GROSS instead."""
    return r.gross if r.sg in ACTIVE else r.cgross


class _Net:
    """Protel logisnet on cancelled rows keeps the net amount; Opera only has
    the lost GROSS, so net is derived with the property's observed tax ratio
    (gross/net over active nights in the extract). Internal only — the
    briefing never shows a derived euro figure on its own."""

    def __init__(self, rows: list[Row]):
        net = sum(r.net for r in rows if r.sg in ACTIVE and r.net > 0)
        gross = sum(r.gross for r in rows if r.sg in ACTIVE and r.net > 0)
        self.ratio = (gross / net) if net else 1.0

    def __call__(self, r: Row) -> float:
        return r.net if r.sg in ACTIVE else (r.cgross / self.ratio if self.ratio else r.cgross)


# ------------------------------------------------------------------
# Pack computation — each function returns rows shaped like the Protel query
# ------------------------------------------------------------------

def compute_pack(rows: list[Row], today: date) -> dict[str, Any]:
    yesterday    = today - timedelta(days=1)
    yday_ly      = _year_ago(yesterday)
    stly_cap     = _year_ago(today)
    stly2_cap    = _year_ago(today, 2)
    mtd_start    = yesterday.replace(day=1)
    mtd_start_ly = _year_ago(mtd_start)
    ty_year, ly_year = today.year, today.year - 1
    net_of = _Net(rows)

    out: dict[str, Any] = {}

    # ── Q1: KPIs ──────────────────────────────────────────────────
    kpi: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.sg in ACTIVE:
            if r.sd == yesterday:
                kpi["rev_yday_ty"] += r.gross; kpi["rev_yday_ty_net"] += r.net; kpi["rn_yday_ty"] += r.rn
            if mtd_start <= r.sd <= yesterday:
                kpi["rev_mtd_ty"] += r.gross; kpi["rev_mtd_ty_net"] += r.net; kpi["rn_mtd_ty"] += r.rn
        if _alive_at(r, stly_cap):
            if r.sd == yday_ly:
                kpi["rev_yday_ly"] += _rev_of(r); kpi["rev_yday_ly_net"] += net_of(r); kpi["rn_yday_ly"] += _rn_of(r)
            if mtd_start_ly <= r.sd <= yday_ly:
                kpi["rev_mtd_ly"] += _rev_of(r); kpi["rev_mtd_ly_net"] += net_of(r); kpi["rn_mtd_ly"] += _rn_of(r)
            if r.sd.year == stly_cap.year and r.bd is not None and r.bd <= stly_cap:
                kpi[f"rn_stly_{r.sd.month}"] += _rn_of(r)
    for m in range(1, 13):
        kpi.setdefault(f"rn_stly_{m}", 0.0)
    out["kpi"] = dict(kpi)

    # ── Q2: In-house yesterday (rooms; Protel counts reservations) ──
    arr = sum(r.arr for r in rows if r.sg in ACTIVE and r.sd == yesterday)
    dep = sum(r.dep for r in rows if r.sg in ACTIVE and r.sd == yesterday)
    occ = sum(r.rn for r in rows if r.sg in ACTIVE and r.sd == yesterday)
    out["inhouse"] = {"arrivals": int(arr), "departures": int(dep),
                      "stayovers": int(max(0, occ - arr))}

    # ── Q3: Pickup — book-date axis, last 7 days incl. today ──────
    seven_ago = today - timedelta(days=6)
    three_ago = today - timedelta(days=2)
    pu: dict[str, float] = defaultdict(float)
    by_month: dict[int, float] = defaultdict(float)
    for r in rows:
        # new bookings AS MADE: every status (a booking later cancelled still
        # counts as +1 here and −1 in the cancellation branch — Protel Q3)
        if r.bd is not None and r.bd >= seven_ago:
            rn, rev = _rn_of(r), _rev_of(r)
            by_month[r.sd.month] += rn
            pu["pickup_7d_rn"] += rn; pu["pickup_7d_rev"] += rev
            if r.bd >= three_ago:
                pu["pickup_3d_rn"] += rn; pu["pickup_3d_rev"] += rev
            if r.bd == yesterday:
                pu["pickup_1d_rn"] += rn; pu["pickup_1d_rev"] += rev
            if r.bd == today:
                pu["pickup_today_rn"] += rn; pu["pickup_today_rev"] += rev
        if r.sg == CANCELLED and r.cd is not None and r.cd >= seven_ago:
            pu["cancel_7d_count"] += r.crn; pu["cancel_7d_rev"] += r.cgross
            if r.cd >= three_ago:
                pu["cancel_3d_count"] += r.crn; pu["cancel_3d_rev"] += r.cgross
            if r.cd == yesterday:
                pu["cancel_1d_count"] += r.crn; pu["cancel_1d_rev"] += r.cgross
            if r.cd == today:
                pu["cancel_today_count"] += r.crn; pu["cancel_today_rev"] += r.cgross
    if by_month:
        top = max(by_month.items(), key=lambda kv: kv[1])
        pu["top_month"], pu["top_month_rn"] = top
    out["pickup"] = dict(pu)

    # ── Q4: Pace — this calendar year by month ────────────────────
    pace: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.sg in ACTIVE and r.sd.year == ty_year:
            p = pace[r.sd.month]
            p["rn_otb_ty"] += r.rn; p["rev_otb_ty"] += r.gross; p["rev_otb_ty_net"] += r.net
        if r.sd.year == ly_year:
            if r.sg in ACTIVE:
                p = pace[r.sd.month]
                p["rn_final_ly"] += r.rn; p["rev_final_ly"] += r.gross; p["rev_final_ly_net"] += r.net
            if _alive_at(r, stly_cap) and r.bd is not None and r.bd <= stly_cap:
                p = pace[r.sd.month]
                p["rn_stly"] += _rn_of(r); p["rev_stly"] += _rev_of(r); p["rev_stly_net"] += net_of(r)
    out["pace"] = [_full(pace[m], _PACE_KEYS, stay_month=m) for m in sorted(pace)]

    # ── Q16: Next-year pace by month ──────────────────────────────
    pn: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.sg in ACTIVE and r.sd.year == ty_year + 1:
            p = pn[r.sd.month]; p["rn_otb"] += r.rn; p["rev_otb"] += r.gross
        if r.sd.year == ty_year and _alive_at(r, stly_cap) and r.bd is not None and r.bd <= stly_cap:
            p = pn[r.sd.month]; p["rn_stly"] += _rn_of(r); p["rev_stly"] += _rev_of(r)
        if r.sd.year == ly_year and _alive_at(r, stly2_cap) and r.bd is not None and r.bd <= stly2_cap:
            p = pn[r.sd.month]; p["rn_stly2"] += _rn_of(r); p["rev_stly2"] += _rev_of(r)
    out["pace_next"] = [_full(pn[m], _PACE_NEXT_KEYS, stay_month=m) for m in sorted(pn)]

    # ── Q5: Sources — full-year revenue vs STLY ───────────────────
    src: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.sg in ACTIVE and r.sd.year == ty_year:
            src[r.src]["rev_ty"] += r.gross; src[r.src]["rn_ty"] += r.rn
        if r.sd.year == ly_year and _alive_at(r, stly_cap) and r.bd is not None and r.bd <= stly_cap:
            src[r.src]["rev_stly"] += _rev_of(r); src[r.src]["rn_stly"] += _rn_of(r)
    out["sources"] = sorted((_full(v, _SOURCE_KEYS, source=s) for s, v in src.items()),
                            key=lambda x: -x["rev_ty"])

    # ── Q6: Next 7 days ───────────────────────────────────────────
    n7: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.sg in ACTIVE and today < r.sd <= today + timedelta(days=7):
            n7[r.sd]["room_nights"] += r.rn; n7[r.sd]["revenue"] += r.gross; n7[r.sd]["arrivals"] += r.arr
    out["next7"] = [dict(stay_date=d, room_nights=v["room_nights"], revenue=v["revenue"],
                         arrivals=int(v["arrivals"])) for d, v in sorted(n7.items())]

    # ── Q8: Booking curves — current + next stay month, TY vs LY ──
    m1, m1yr = today.month, today.year
    m2 = today.month + 1 if today.month < 12 else 1
    m2yr = today.year if today.month < 12 else today.year + 1
    targets = {(m1, m1yr): "TY", (m2, m2yr): "TY", (m1, m1yr - 1): "LY", (m2, m2yr - 1): "LY"}
    cv: dict[tuple, float] = defaultdict(float)
    for r in rows:
        if r.sg not in ACTIVE or r.bd is None:
            continue
        period = targets.get((r.sd.month, r.sd.year))
        if period is None:
            continue
        raw = (_eom(r.sd) - r.bd).days
        bucket = 0 if raw < 0 else 200 if raw > 200 else (raw // 30) * 30
        cv[(r.sd.month, r.sd.year, period, bucket)] += r.gross
    out["curve"] = [dict(stay_month=k[0], stay_year=k[1], period=k[2], days_bucket=k[3], revenue=v)
                    for k, v in sorted(cv.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2], -kv[0][3]))]

    # ── Q8b: Full-year curve by book month ────────────────────────
    cf: dict[tuple, float] = defaultdict(float)
    for r in rows:
        if r.sg not in ACTIVE or r.bd is None:
            continue
        if r.sd.year == ty_year and r.bd <= today:
            cf[(r.bd.month, "TY")] += r.gross
        elif r.sd.year == ly_year:
            cf[(r.bd.month, "LY")] += r.gross
    out["curve_full"] = [dict(book_month=k[0], period=k[1], revenue=v)
                         for k, v in sorted(cf.items(), key=lambda kv: (kv[0][1], kv[0][0]))]

    # ── Q9: Daily NET pickup — last 14 days × stay month ──────────
    win_start = today - timedelta(days=13)
    pd: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.bd is not None and win_start <= r.bd <= today:
            k = (r.bd, r.sd.month, r.sd.year)
            pd[k]["net_rn"] += _rn_of(r); pd[k]["net_rev"] += _rev_of(r)
        if r.sg == CANCELLED and r.cd is not None and win_start <= r.cd <= today:
            k = (r.cd, r.sd.month, r.sd.year)
            pd[k]["net_rn"] -= r.crn; pd[k]["net_rev"] -= r.cgross
    out["pickup_daily"] = [dict(ref_date=k[0], stay_month=k[1], stay_year=k[2], **v)
                           for k, v in sorted(pd.items())]

    # ── Q10: OTB by stay date — next 90 days + STLY same lead time ─
    horizon = today + timedelta(days=90)
    ly_end = stly_cap + timedelta(days=90)
    ty_otb: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    stly_otb: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.sg in ACTIVE and today <= r.sd <= horizon:
            ty_otb[r.sd]["rn"] += r.rn; ty_otb[r.sd]["rev"] += r.gross
        if (stly_cap <= r.sd <= ly_end and _alive_at(r, stly_cap)
                and r.bd is not None and r.bd <= stly_cap):
            k = _year_ahead(r.sd)
            stly_otb[k]["rn"] += _rn_of(r); stly_otb[k]["rev"] += _rev_of(r)
    out["otb_by_date"] = [dict(stay_date=d, rn_ty=v["rn"], rev_ty=v["rev"],
                               rn_stly=stly_otb[d]["rn"] if d in stly_otb else 0,
                               rev_stly=stly_otb[d]["rev"] if d in stly_otb else 0)
                          for d, v in sorted(ty_otb.items())]

    # ── Q11: Current month remaining nights ───────────────────────
    cm: dict[str, float] = defaultdict(float)
    for r in rows:
        if (r.sg in ACTIVE and r.sd.year == today.year and r.sd.month == today.month and r.sd >= today):
            cm["rn_remaining_otb_ty"] += r.rn; cm["rev_remaining_otb_ty"] += r.gross
        if r.sd.year == stly_cap.year and r.sd.month == stly_cap.month and r.sd >= stly_cap:
            if _alive_at(r, stly_cap) and r.bd is not None and r.bd <= stly_cap:
                cm["rn_remaining_stly"] += _rn_of(r); cm["rev_remaining_stly"] += _rev_of(r)
            if r.sg in ACTIVE:
                cm["rn_remaining_final_ly"] += r.rn; cm["rev_remaining_final_ly"] += r.gross
    out["current_month"] = dict(cm)

    # ── Q13: Lead time — bookings made in the last 28 days vs LY ──
    win28 = today - timedelta(days=27)
    ly_today, ly_win28 = _year_ago(today), _year_ago(win28)
    lt: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.bd is None or r.sd < r.bd:
            continue
        lead = (r.sd - r.bd).days
        if r.sg in ACTIVE and win28 <= r.bd <= today:
            k = ("TY", r.sd.month, r.sd.year, r.src, _lead_bucket(lead))
            lt[k]["room_nights"] += r.rn; lt[k]["revenue"] += r.gross; lt[k]["lead_days_x_rn"] += lead * r.rn
        elif ly_win28 <= r.bd <= ly_today and _alive_at(r, ly_today):
            k = ("LY", r.sd.month, r.sd.year, r.src, _lead_bucket(lead))
            rn = _rn_of(r)
            lt[k]["room_nights"] += rn; lt[k]["revenue"] += _rev_of(r); lt[k]["lead_days_x_rn"] += lead * rn
    out["lead_time"] = [dict(period=k[0], stay_month=k[1], stay_year=k[2], source=k[3], lead_bucket=k[4], **v)
                        for k, v in sorted(lt.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][1]))]

    # ── Q14: Daily cancellations — last 14 days × stay month ──────
    cd_rows: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.sg == CANCELLED and r.cd is not None and win_start <= r.cd <= today:
            k = (r.cd, r.sd.month, r.sd.year)
            cd_rows[k]["cancel_rn"] += r.crn; cd_rows[k]["cancel_rev"] += r.cgross
    out["cancel_daily"] = [dict(ref_date=k[0], stay_month=k[1], stay_year=k[2], **v)
                           for k, v in sorted(cd_rows.items())]

    # ── Q15: Consumed nights + revenue by source, MTD vs LY-364d ──
    ly_start = mtd_start - timedelta(days=364)
    ly_end_c = yesterday - timedelta(days=364)
    cs: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.sg not in ACTIVE or not r.has_rev:
            continue
        if mtd_start <= r.sd <= yesterday:
            cs[("TY", r.src)]["room_nights"] += r.rn; cs[("TY", r.src)]["revenue"] += r.gross
        elif ly_start <= r.sd <= ly_end_c:
            cs[("LY", r.src)]["room_nights"] += r.rn; cs[("LY", r.src)]["revenue"] += r.gross
    out["consumed"] = [dict(period=k[0], source=k[1], **v)
                       for k, v in sorted(cs.items(), key=lambda kv: (kv[0][0], -kv[1]["revenue"]))]

    return out


# ------------------------------------------------------------------
# Fetch
# ------------------------------------------------------------------

def _load_rows(cur, resort: str) -> list[Row]:
    cur.arraysize = 5000
    cur.execute(Q.Q_GRAIN, resort=resort)
    return [Row(_d(sd), _d(bd), _d(cd), src or "Direct", sg, int(hr or 0),
                float(rn or 0), float(net or 0), float(gross or 0),
                float(crn or 0), float(cgross or 0), float(arr or 0), float(dep or 0))
            for sd, bd, cd, src, sg, hr, rn, net, gross, crn, cgross, arr, dep in cur.fetchall()]


def _check_night_audit(cur, resort: str, yesterday: date) -> None:
    cur.execute(Q.Q_BUSINESS_DATE, resort=resort, day=yesterday)
    states = {_d(bd): st for bd, st in cur.fetchall()}
    state = states.get(yesterday)
    if state != "CLOSED":
        raise RuntimeError(
            f"Opera night audit for {yesterday} on resort {resort} is not closed "
            f"(state={state!r}); room revenue is posted at audit — retry later")


def fetch_snapshot(conn, hotel_ctx: dict[str, Any]) -> dict[str, Any]:
    # `_today` is an OPTIONAL override for the validation harness (replay a
    # closed business day); the scheduler never sets it.
    today = hotel_ctx.get("_today") or date.today()
    yesterday = today - timedelta(days=1)
    resort = str(hotel_ctx["pms_hotel_id"])
    total_rooms = int(hotel_ctx["total_rooms"])
    cur = conn.cursor()

    _check_night_audit(cur, resort, yesterday)

    cur.execute(Q.Q_INVENTORY, resort=resort)
    pms_rooms = int((cur.fetchone() or [0])[0] or 0) or total_rooms

    rows = _load_rows(cur, resort)
    pack = compute_pack(rows, today)

    ref_dates = [yesterday, yesterday - timedelta(days=365), _year_ago(yesterday)] + \
                [today + timedelta(days=i) for i in range(0, 8)]
    pack["inventory"] = [{"ref_date": d, "total_rooms": pms_rooms} for d in ref_dates]

    payload = assemble_snapshot(pack, hotel_ctx, today=today)
    payload["pms_meta"] = {"pms": "opera_oracle", "resort": resort,
                           "grain_rows": len(rows), "pms_rooms": pms_rooms}
    return payload
