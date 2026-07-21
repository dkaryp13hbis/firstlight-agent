"""
Runs all SQL queries and returns a structured dict that mirrors the JSX HOTEL_DATA shape.
All derived values (ADR, occupancy %, variance) are calculated here in Python.
"""

import calendar
from datetime import date, datetime, timedelta
from typing import Any

import pyodbc

import config
from db import queries as Q


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _adr(revenue: float, room_nights: float) -> float:
    return round(revenue / room_nights, 2) if room_nights else 0.0


def _occ(room_nights: float, inventory: float) -> float:
    return round(room_nights / inventory, 4) if inventory else 0.0


def _var_pct(ty: float, ly: float) -> float | None:
    return round((ty - ly) / ly, 4) if ly else None


def _pace_status(occ_ty: float, occ_stly: float) -> str:
    if occ_stly == 0:
        return "on_track"
    diff = (occ_ty - occ_stly) / occ_stly
    if diff >= 0.05:
        return "ahead"
    if diff <= -0.05:
        return "behind"
    return "on_track"


def _month_abbr(month_num: int) -> str:
    return calendar.month_abbr[month_num]


def _build_curve_days(rows: list[dict], today_days_bucket: int) -> dict:
    """
    Build cumulative booking revenue curve using 'days before EOM' x-axis.
    rows must have {days_bucket (int, DESC sorted), period, revenue}.
    today_days_bucket: the rounded 30-day bucket equivalent to today for this stay month.
    Curve goes from far-out (300d) toward 0d (left→right).
    """
    ty: dict[int, float] = {}
    ly: dict[int, float] = {}
    for r in rows:
        bucket = int(r["days_bucket"])
        rev = float(r["revenue"] or 0)
        if r["period"] == "TY":
            ty[bucket] = ty.get(bucket, 0.0) + rev
        else:
            ly[bucket] = ly.get(bucket, 0.0) + rev

    # All buckets DESC (300 → 0), cumulate running total
    all_buckets = sorted(set(list(ty.keys()) + list(ly.keys())), reverse=True)

    ty_cum, ly_cum = 0.0, 0.0
    ty_points: list[dict] = []
    stly_points: list[dict] = []
    stly_after: list[dict] = []

    for b in all_buckets:
        lbl = f"{b}d"
        if b in ty:
            ty_cum += ty[b]
            ty_points.append({"label": lbl, "days": b, "v": round(ty_cum, 0)})
        if b in ly:
            ly_cum += ly[b]
            entry = {"label": lbl, "days": b, "v": round(ly_cum, 0)}
            # STLY line up to same-relative-position as today; after = faded
            if b >= today_days_bucket:
                stly_points.append(entry)
            else:
                stly_after.append(entry)

    return {
        "ty":         ty_points,
        "stly":       stly_points,
        "stly_after": stly_after,
        "final_ly":   round(ly_cum, 0),
        "mode":       "days",
    }


def _build_curve_months(rows: list[dict], stly_cap_year: int, stly_cap_month: int) -> dict:
    """
    Build cumulative booking revenue curve using calendar-month x-axis (for full-year view).
    rows must have {book_year, book_month, period, revenue}.
    """
    ty: dict[tuple, float] = {}
    ly: dict[tuple, float] = {}
    for r in rows:
        key = (int(r["book_year"]), int(r["book_month"]))
        rev = float(r["revenue"] or 0)
        if r["period"] == "TY":
            ty[key] = ty.get(key, 0.0) + rev
        else:
            ly[key] = ly.get(key, 0.0) + rev

    all_keys = sorted(set(list(ty.keys()) + list(ly.keys())))
    ty_cum, ly_cum = 0.0, 0.0
    ty_points: list[dict] = []
    stly_points: list[dict] = []
    stly_after: list[dict] = []

    for k in all_keys:
        lbl = calendar.month_abbr[k[1]]
        if k in ty:
            ty_cum += ty[k]
            ty_points.append({"label": lbl, "v": round(ty_cum, 0)})
        if k in ly:
            ly_cum += ly[k]
            before_cap = (k[0] < stly_cap_year) or (
                k[0] == stly_cap_year and k[1] <= stly_cap_month
            )
            entry = {"label": lbl, "v": round(ly_cum, 0)}
            if before_cap:
                stly_points.append(entry)
            else:
                stly_after.append(entry)

    return {
        "ty":         ty_points,
        "stly":       stly_points,
        "stly_after": stly_after,
        "final_ly":   round(ly_cum, 0),
        "mode":       "months",
    }


def _build_curve_months_by_bookmonth(rows: list[dict], stly_cap_month: int) -> dict:
    """
    Build cumulative booking revenue curve using book-month (1-12) as x-axis.
    Both TY and STLY share the same month labels. STLY splits at stly_cap_month.
    """
    ty: dict[int, float] = {}
    ly: dict[int, float] = {}
    for r in rows:
        m   = int(r["book_month"])
        rev = float(r["revenue"] or 0)
        if r["period"] == "TY":
            ty[m] = ty.get(m, 0.0) + rev
        else:
            ly[m] = ly.get(m, 0.0) + rev

    ty_cum, ly_cum = 0.0, 0.0
    ty_points:   list[dict] = []
    stly_points: list[dict] = []
    stly_after:  list[dict] = []

    for m in range(1, 13):
        lbl = calendar.month_abbr[m]
        if m in ty:
            ty_cum += ty[m]
            ty_points.append({"label": lbl, "v": round(ty_cum, 0)})
        if m in ly:
            ly_cum += ly[m]
            entry = {"label": lbl, "v": round(ly_cum, 0)}
            if m <= stly_cap_month:
                stly_points.append(entry)
            else:
                stly_after.append(entry)

    return {
        "ty":         ty_points,
        "stly":       stly_points,
        "stly_after": stly_after,
        "final_ly":   round(ly_cum, 0),
        "mode":       "months",
    }


def _rows(cursor: pyodbc.Cursor) -> list[dict]:
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# ------------------------------------------------------------------
# Main fetch
# ------------------------------------------------------------------

def fetch_briefing_data(conn: pyodbc.Connection) -> dict[str, Any]:
    cur = conn.cursor()
    today = date.today()
    yesterday = today - timedelta(days=1)
    hotel_id = config.HOTEL_ID

    # ── Q1: KPIs ──────────────────────────────────────────────────
    cur.execute(Q.Q_KPIS, hotel_id, hotel_id, hotel_id)
    kpi = _rows(cur)[0]

    rev_yd_ty  = float(kpi["rev_yday_ty"] or 0)
    rev_yd_ly  = float(kpi["rev_yday_ly"] or 0)
    rn_yd_ty   = float(kpi["rn_yday_ty"]  or 0)
    rn_yd_ly   = float(kpi["rn_yday_ly"]  or 0)
    rev_mtd_ty = float(kpi["rev_mtd_ty"]  or 0)
    rev_mtd_ly = float(kpi["rev_mtd_ly"]  or 0)
    rn_mtd_ty  = float(kpi["rn_mtd_ty"]   or 0)
    rn_mtd_ly  = float(kpi["rn_mtd_ly"]   or 0)
    rn_stly_by_month = {m: float(kpi[f"rn_stly_{m}"] or 0) for m in range(1, 13)}

    # ── Q7: Inventory ─────────────────────────────────────────────
    cur.execute(Q.Q_INVENTORY, hotel_id)
    inv_rows = {r["ref_date"]: r["total_rooms"] for r in _rows(cur)}

    inv_yday    = inv_rows.get(yesterday, config.TOTAL_ROOMS)
    inv_yday_ly = inv_rows.get(yesterday - timedelta(days=365), config.TOTAL_ROOMS)

    # MTD inventory = sum of available rooms from month start to yesterday
    mtd_start = yesterday.replace(day=1)
    days_mtd = (yesterday - mtd_start).days + 1
    inv_mtd_ty = inv_yday * days_mtd          # approximation using today's room count
    inv_mtd_ly = inv_yday_ly * days_mtd

    # ── Q2: In-house ──────────────────────────────────────────────
    cur.execute(Q.Q_INHOUSE, hotel_id)
    ih = _rows(cur)[0]
    arrivals   = ih["arrivals"]   or 0
    departures = ih["departures"] or 0
    stayovers  = ih["stayovers"]  or 0

    # ── Q3: Pickup ────────────────────────────────────────────────
    cur.execute(Q.Q_PICKUP, hotel_id, hotel_id)
    pu = _rows(cur)[0]
    pickup_today_rn  = float(pu["pickup_today_rn"]  or 0)
    pickup_today_rev = float(pu["pickup_today_rev"] or 0)
    pickup_1d_rn     = float(pu["pickup_1d_rn"]     or 0)
    pickup_1d_rev    = float(pu["pickup_1d_rev"]    or 0)
    pickup_3d_rn     = float(pu["pickup_3d_rn"]     or 0)
    pickup_3d_rev    = float(pu["pickup_3d_rev"]    or 0)
    pickup_7d_rn     = float(pu["pickup_7d_rn"]     or 0)
    pickup_7d_rev    = float(pu["pickup_7d_rev"]    or 0)
    cancel_today_count = float(pu["cancel_today_count"] or 0)
    cancel_today_rev   = float(pu["cancel_today_rev"]   or 0)
    cancel_1d_count  = float(pu["cancel_1d_count"]  or 0)
    cancel_1d_rev    = float(pu["cancel_1d_rev"]    or 0)
    cancel_3d_count  = float(pu["cancel_3d_count"]  or 0)
    cancel_3d_rev    = float(pu["cancel_3d_rev"]     or 0)
    cancel_7d_count  = float(pu["cancel_7d_count"]  or 0)
    cancel_7d_rev    = float(pu["cancel_7d_rev"]     or 0)
    top_month_num    = int(pu["top_month"]           or today.month)
    top_month_rn     = float(pu["top_month_rn"]      or 0)

    # ── Q4: Pace ──────────────────────────────────────────────────
    cur.execute(Q.Q_PACE, hotel_id, hotel_id, hotel_id)
    pace_rows = _rows(cur)

    pace = []
    for r in pace_rows:
        m = r["stay_month"]
        rn_ty   = float(r["rn_otb_ty"]   or 0)
        rn_stly = float(r["rn_stly"]     or 0)
        rn_fly  = float(r["rn_final_ly"] or 0)
        rev_ty   = float(r["rev_otb_ty"]  or 0)
        rev_stly = float(r["rev_stly"]    or 0)
        rev_fly  = float(r["rev_final_ly"] or 0)
        # inventory for that month = days_in_month × total_rooms
        days = calendar.monthrange(today.year, m)[1]
        inv = inv_yday * days
        occ_ty   = _occ(rn_ty,   inv)
        occ_stly = _occ(rn_stly, inv)
        occ_fly  = _occ(rn_fly,  inv)
        pace.append({
            "month":     _month_abbr(m),
            "month_num": m,
            "occ":       occ_ty,
            "stly":      occ_stly,
            "final":     occ_fly,
            "status":    _pace_status(occ_ty, occ_stly),
            "rev":       round(rev_ty, 0),
            "rev_stly":  round(rev_stly, 0),
            "rev_final": round(rev_fly, 0),
            "rn":        int(rn_ty),
            "rn_stly":   int(rn_stly),
            "rn_final_ly": int(rn_fly),
            "adr":          _adr(rev_ty, rn_ty),
            "adr_stly":     _adr(rev_stly, rn_stly),
            "adr_final_ly": _adr(rev_fly, rn_fly),
        })

    pace_current = [p for p in pace if p["month_num"] >= today.month][:3]

    # ── Q5: Sources OTB ───────────────────────────────────────────
    cur.execute(Q.Q_SOURCES_OTB, hotel_id, hotel_id)
    ch_rows = _rows(cur)
    total_rev_ty = sum(float(r["rev_ty"] or 0) for r in ch_rows) or 1
    channels = []
    for r in ch_rows[:6]:
        rev_ty   = float(r["rev_ty"]   or 0)
        rev_stly = float(r["rev_stly"] or 0)
        var      = _var_pct(rev_ty, rev_stly)
        channels.append({
            "name":     r["source"],
            "rev":      round(rev_ty, 0),
            "rev_stly": round(rev_stly, 0),
            "nights":   int(r["rn_ty"] or 0),
            "pct":      round(rev_ty / total_rev_ty, 4),
            "var":      var,
            "trend":    "up" if rev_ty >= rev_stly else "down",
        })

    # ── Q6: Next 7 days ───────────────────────────────────────────
    cur.execute(Q.Q_NEXT7, hotel_id)
    next7_rows = _rows(cur)
    next7 = []
    for r in next7_rows:
        d_date = r["stay_date"]
        if isinstance(d_date, str):
            d_date = datetime.strptime(d_date, "%Y-%m-%d").date()
        inv_day = inv_rows.get(d_date, config.TOTAL_ROOMS)
        rn  = float(r["room_nights"] or 0)
        rev = float(r["revenue"]     or 0)
        next7.append({
            "date":     f"{d_date.day} {d_date.strftime('%b')}",
            "dow":      d_date.strftime("%a"),
            "occ":      _occ(rn, inv_day),
            "rooms":    int(rn),
            "rev":      round(rev, 0),
            "adr":      _adr(rev, rn),
            "arrivals": r["arrivals"] or 0,
        })

    # ── Q8: Booking curves ────────────────────────────────────────
    stly_cap = today - timedelta(days=365)

    cur.execute(Q.Q_BOOKING_CURVE, hotel_id, hotel_id, hotel_id, hotel_id)
    curve_rows = _rows(cur)

    m1, m1yr = today.month, today.year
    m2 = today.month + 1 if today.month < 12 else 1
    m2yr = today.year if today.month < 12 else today.year + 1

    # Compute "days before EOM" bucket for today — same for both TY and STLY
    def _eom_days_bucket(ref_date: date, stay_month: int, stay_year: int) -> int:
        last_day = calendar.monthrange(stay_year, stay_month)[1]
        eom = date(stay_year, stay_month, last_day)
        d = max(0, min(200, (eom - ref_date).days))
        return (d // 30) * 30

    today_bucket_m1 = _eom_days_bucket(today, m1, m1yr)
    today_bucket_m2 = _eom_days_bucket(today, m2, m2yr)

    rows_m1 = [r for r in curve_rows if int(r["stay_month"]) == m1 and
               ((r["period"] == "TY" and int(r["stay_year"]) == m1yr) or
                (r["period"] == "LY" and int(r["stay_year"]) == m1yr - 1))]
    rows_m2 = [r for r in curve_rows if int(r["stay_month"]) == m2 and
               ((r["period"] == "TY" and int(r["stay_year"]) == m2yr) or
                (r["period"] == "LY" and int(r["stay_year"]) == m2yr - 1))]

    curve_current = _build_curve_days(rows_m1, today_bucket_m1)
    curve_current["month_label"] = f"{_month_abbr(m1)} {m1yr}"

    curve_next = _build_curve_days(rows_m2, today_bucket_m2)
    curve_next["month_label"] = f"{_month_abbr(m2)} {m2yr}"

    # Full-year booking curve (calendar book-month x-axis)
    cur.execute(Q.Q_BOOKING_CURVE_FULL_MONTHS, hotel_id, hotel_id)
    fy_rows = _rows(cur)
    curve_full = _build_curve_months_by_bookmonth(fy_rows, stly_cap.month)
    curve_full["month_label"] = f"Full Year {today.year}"

    # ── Q9: Daily pickup for last 14 days by future stay month ───
    cur.execute(Q.Q_PICKUP_DAILY, hotel_id, hotel_id)
    pickup_daily = []
    for r in _rows(cur):
        ref = r["ref_date"]
        if hasattr(ref, "strftime"):
            ref = ref.strftime("%Y-%m-%d")
        pickup_daily.append({
            "ref_date":   str(ref),
            "stay_month": int(r["stay_month"]),
            "stay_year":  int(r["stay_year"]),
            "net_rn":     int(r["net_rn"]  or 0),
            "net_rev":    round(float(r["net_rev"] or 0), 0),
        })

    # ── Q10: OTB by stay date — next 90 days ─────────────────────
    cur.execute(Q.Q_OTB_BY_DATE_90, hotel_id, hotel_id)
    otb_by_date = []
    for r in _rows(cur):
        sd = r["stay_date"]
        if hasattr(sd, "strftime"):
            sd = sd.strftime("%Y-%m-%d")
        otb_by_date.append({
            "stay_date": str(sd),
            "rn_ty":     int(r["rn_ty"]    or 0),
            "rev_ty":    round(float(r["rev_ty"]   or 0), 0),
            "rn_stly":   int(r["rn_stly"]  or 0),
            "rev_stly":  round(float(r["rev_stly"] or 0), 0),
        })

    # ── Q11: Current month remaining nights ───────────────────────
    cur.execute(Q.Q_CURRENT_MONTH_REMAINING, hotel_id)
    cm_rows = _rows(cur)
    cm = cm_rows[0] if cm_rows else {}
    current_month_remaining = {
        "rn_remaining_otb_ty":    int(cm.get("rn_remaining_otb_ty",    0) or 0),
        "rev_remaining_otb_ty":   round(float(cm.get("rev_remaining_otb_ty",   0) or 0), 0),
        "rn_remaining_stly":      int(cm.get("rn_remaining_stly",      0) or 0),
        "rev_remaining_stly":     round(float(cm.get("rev_remaining_stly",     0) or 0), 0),
        "rn_remaining_final_ly":  int(cm.get("rn_remaining_final_ly",  0) or 0),
        "rev_remaining_final_ly": round(float(cm.get("rev_remaining_final_ly", 0) or 0), 0),
    }

    # ── Assemble payload ──────────────────────────────────────────
    return {
        "hotel_name": config.HOTEL_NAME,
        "report_date": f"{yesterday.strftime('%A, %B')} {yesterday.day}, {yesterday.year}",
        "generated_at": datetime.now().strftime("%H:%M"),

        "yesterday": {
            "revenue":      round(rev_yd_ty, 0),
            "revenueLY":    round(rev_yd_ly, 0),
            "roomNights":   int(rn_yd_ty),
            "roomNightsLY": int(rn_yd_ly),
            "adr":          _adr(rev_yd_ty, rn_yd_ty),
            "adrLY":        _adr(rev_yd_ly, rn_yd_ly),
            "occupancy":    _occ(rn_yd_ty, inv_yday),
            "occupancyLY":  _occ(rn_yd_ly, inv_yday_ly),
            "arrivals":     arrivals,
            "departures":   departures,
            "stayovers":    stayovers,
            "inHouse":      arrivals + stayovers,
        },

        "mtd": {
            "revenue":      round(rev_mtd_ty, 0),
            "revenueLY":    round(rev_mtd_ly, 0),
            "roomNights":   int(rn_mtd_ty),
            "roomNightsLY": int(rn_mtd_ly),
            "adr":          _adr(rev_mtd_ty, rn_mtd_ty),
            "adrLY":        _adr(rev_mtd_ly, rn_mtd_ly),
            "occupancy":    _occ(rn_mtd_ty, inv_mtd_ty),
            "occupancyLY":  _occ(rn_mtd_ly, inv_mtd_ly),
            "month_name":   yesterday.strftime("%B"),
        },

        "pickup": {
            "today": {
                "roomNights": int(pickup_today_rn),
                "revenue":    round(pickup_today_rev, 0),
            },
            "last1d": {
                "roomNights": int(pickup_1d_rn),
                "revenue":    round(pickup_1d_rev, 0),
            },
            "last3d": {
                "roomNights": int(pickup_3d_rn),
                "revenue":    round(pickup_3d_rev, 0),
            },
            "last7d": {
                "roomNights": int(pickup_7d_rn),
                "revenue":    round(pickup_7d_rev, 0),
            },
            "topMonth":           _month_abbr(top_month_num),
            "topMonthNights":     int(top_month_rn),
            "cancellationsToday":        int(cancel_today_count),
            "cancellationRevenueToday":  round(cancel_today_rev, 0),
            "cancellations1d":    int(cancel_1d_count),
            "cancellationRevenue": round(cancel_1d_rev, 0),
            "cancellations3d":       int(cancel_3d_count),
            "cancellationRevenue3d": round(cancel_3d_rev, 0),
            "cancellations7d":       int(cancel_7d_count),
            "cancellationRevenue7d": round(cancel_7d_rev, 0),
            "date1d": yesterday.strftime("%d/%m"),
            "date3d": f"{(today - timedelta(days=2)).strftime('%d/%m')}–{today.strftime('%d/%m')}",
            "date7d": f"{(today - timedelta(days=6)).strftime('%d/%m')}–{today.strftime('%d/%m')}",
        },

        "pace":        pace,
        "pace_current": pace_current,
        "pickup_daily": pickup_daily,
        "otb_by_date": otb_by_date,
        "current_month_remaining": current_month_remaining,
        "stly_occ_by_month": {
            m: _occ(rn_stly_by_month[m], inv_yday * calendar.monthrange(today.year - 1, m)[1])
            for m in range(1, 13)
        },
        "topChannels": channels,
        "next7days":   next7,
        "curves": {
            "current": curve_current,
            "next":    curve_next,
            "full":    curve_full,
        },
    }
