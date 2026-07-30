"""
Chart series for the app briefing — the four decided mobile chart forms
(specs in ENGINEERING_LOG §8, decided 2026-07-28 with the user):

  meter     Curve position meter   → under the OTB charts section
  velocity  Booking speed 7d/14d   → under the Pickup section
  butterfly Booked vs cancelled    → replaces "Top month" in Pickup
  heat      Demand heat 60 days    → under the OTB section

Pure compute: returns display-ready dicts (formatted strings + bar widths);
the Jinja template only does markup. Every chart is None when its data is
absent (legacy payloads) — the template skips it. App HTML only, not email.
"""

import calendar
from datetime import date as _date, datetime as _dt, timedelta as _td
from decimal import Decimal as D, getcontext
from typing import Any

getcontext().prec = 28

NAVY = "#0F2860"; BLUE = "#2E7CF7"; GREEN = "#1A7A50"; RED = "#B83A1B"
AMBER = "#B47D09"; SOFT = "#A0AAC0"
# Heat ramp: single-hue BLUE (user decision 2026-07-30 — keep the app's blue,
# 8b layout/ring design retained; the purple re-tokenisation was declined)
RAMP = ["#F2F2F7", "#E1EBFB", "#C4DAF9", "#9FC4F6", "#6FA7F2", "#3D87EE", "#0A6CDF"]


def _fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", ".")


def _meter(data: dict) -> dict | None:
    today = _date.today()
    rows = []
    for p in data.get("pace", []):
        if not isinstance(p, dict) or p.get("month_num", 0) < today.month:
            continue
        rn, sly, fin = p.get("rn"), p.get("rn_stly"), p.get("rn_final_ly")
        if not fin or rn is None or sly is None:
            continue
        pct, spct = rn / fin * 100, sly / fin * 100
        d, togo = rn - sly, fin - rn
        beat = togo < 0
        rows.append({
            "m": p["month"],
            "bw": round(min(pct, 100), 1),
            "tx": round(min(spct, 98), 1),
            "bar_col": RED if d < 0 else NAVY,
            "bar_radius": "9px 0 0 9px" if beat else "9px",
            "over_w": round(min(max(pct - 100, 0), 20) / 100 * 82, 1) if beat else 0,
            "rn_txt": _fmt(rn), "sly_txt": _fmt(sly), "fin_txt": _fmt(fin),
            "d_txt": ("+" if d >= 0 else "−") + _fmt(abs(d)),
            "d_col": GREEN if d > 0 else (RED if d < 0 else SOFT),
            "beat": beat,
            "second": (f"+{_fmt(-togo)} rn above LY final" if beat
                       else f"{_fmt(togo)} rn to reach LY final"),
        })
    return {"rows": rows} if rows else None


def _velocity(data: dict) -> dict | None:
    today = _date.today()
    pd_rows = data.get("pickup_daily") or []
    lt = data.get("lead_time") or []
    if not pd_rows:
        return None
    # Calendar windows (same convention as the butterfly)
    end = max(r["ref_date"] for r in pd_rows)
    end_d = _dt.strptime(end[:10], "%Y-%m-%d").date()
    lo7, lo14 = (end_d - _td(days=6)).isoformat(), (end_d - _td(days=13)).isoformat()
    d7 = {r["ref_date"] for r in pd_rows if r["ref_date"] >= lo7}
    d14 = {r["ref_date"] for r in pd_rows if r["ref_date"] >= lo14}

    def ly_rate(m):
        rn = sum(r.get("rn", 0) for r in lt if r.get("period") == "LY"
                 and r.get("stay_month") == m and r.get("stay_year") == today.year - 1)
        return rn / 28.0

    raw = []
    for k in range(4):  # current + next 3
        m = (today.month - 1 + k) % 12 + 1
        y = today.year + ((today.month - 1 + k) // 12)
        p = next((p for p in data.get("pace", []) if p.get("month_num") == m), None)
        if not p:
            continue
        v7 = sum(r["net_rn"] for r in pd_rows if r["stay_month"] == m
                 and r["stay_year"] == y and r["ref_date"] in d7) / 7.0
        v14 = sum(r["net_rn"] for r in pd_rows if r["stay_month"] == m
                  and r["stay_year"] == y and r["ref_date"] in d14) / 14.0
        fin, rn = p.get("rn_final_ly") or 0, p.get("rn") or 0
        m_end = _date(y, m, calendar.monthrange(y, m)[1])
        days_left = max((m_end - today).days, 1)
        passed = fin > 0 and rn >= fin
        needed = 0.0 if passed or fin == 0 else max(fin - rn, 0) / days_left
        raw.append({"m": p["month"], "v7": v7, "v14": v14, "ly": ly_rate(m),
                    "needed": needed, "passed": passed, "over": rn - fin})
    if not raw:
        return None
    mx = max(max(r["v7"], r["v14"], r["ly"], r["needed"]) for r in raw) * 1.12 or 1

    def W(v):
        return round(max(min(v / mx * 100, 100), 0), 1)

    rows = []
    for r in raw:
        accel = r["v7"] - r["v14"]
        rows.append({
            "m": r["m"],
            "w7": W(r["v7"]), "w14": W(r["v14"]),
            "lyx": W(r["ly"]),
            "needx": W(r["needed"]) if not r["passed"] else None,
            "v7_txt": f"{r['v7']:.1f}", "v14_txt": f"{r['v14']:.1f}",
            "passed": r["passed"],
            "under": (f"✓ passed LY final (+{_fmt(r['over'])} rn)" if r["passed"]
                      else f"need {r['needed']:.1f}/day to reach LY final"),
            "accel_txt": ("speeding up" if accel > 0.5 else
                          "slowing down" if accel < -0.5 else "steady"),
            "accel_col": GREEN if accel > 0.5 else (RED if accel < -0.5 else SOFT),
        })
    return {"rows": rows}


def _butterfly(data: dict) -> dict | None:
    pd_rows = data.get("pickup_daily") or []
    cd_rows = data.get("cancel_daily") or []
    if not pd_rows:
        return None
    # CALENDAR windows anchored on the newest booking ref_date — bookings and
    # cancellations MUST share the same 7/14-day spans (distinct-date windows
    # drifted: cancels once spanned 8 calendar days vs bookings' 7).
    end = max(r["ref_date"] for r in pd_rows)
    end_d = _dt.strptime(end[:10], "%Y-%m-%d").date()
    lo7, lo14 = (end_d - _td(days=6)).isoformat(), (end_d - _td(days=13)).isoformat()

    agg: dict = {}
    for r in pd_rows:
        k = (r["stay_year"], r["stay_month"])
        a = agg.setdefault(k, {"n7": 0, "n14": 0})
        if r["ref_date"] >= lo14:
            a["n14"] += r["net_rn"]
            if r["ref_date"] >= lo7:
                a["n7"] += r["net_rn"]
    raw = []
    for (y, m), a in sorted(agg.items()):
        c14 = sum(r["cancel_rn"] for r in cd_rows
                  if r["stay_month"] == m and r["stay_year"] == y
                  and lo14 <= r["ref_date"] <= end)
        c7 = sum(r["cancel_rn"] for r in cd_rows
                 if r["stay_month"] == m and r["stay_year"] == y
                 and lo7 <= r["ref_date"] <= end)
        raw.append({"m": calendar.month_abbr[m], "c7": c7, "c14": c14,
                    "b7": max(a["n7"] + c7, 0), "b14": max(a["n14"] + c14, 0),
                    "n7": a["n7"], "n14": a["n14"]})
    raw = raw[:5]
    if not raw:
        return None
    mx = max(max(r["c14"], r["b14"], r["c7"], r["b7"]) for r in raw) * 1.08 or 1
    rows = []
    for r in raw:
        warn = r["c14"] > 0.6 * r["b14"] if r["b14"] else False
        rows.append({
            "m": r["m"],
            "lw7": round(r["c7"] / mx * 100, 1), "rw7": round(r["b7"] / mx * 100, 1),
            "lw14": round(r["c14"] / mx * 100, 1), "rw14": round(r["b14"] / mx * 100, 1),
            "n7_txt": f"{r['n7']:+d}", "n14_txt": f"{r['n14']:+d}",
            "n7_col": GREEN if r["n7"] >= 0 else RED,
            "n14_col": RED if (r["n14"] < 0 or warn) else GREEN,
        })
    worst = max(raw, key=lambda r: (r["c14"] / r["b14"]) if r["b14"] else 0)
    alert = None
    if worst["b14"]:
        share = worst["c14"] / worst["b14"] * 100
        if share >= 15:
            alert = (f"{worst['m']}: cancellations are {share:.0f}% of the rooms "
                     f"booked in the last 14 days — the highest churn of any month.")
    return {"rows": rows, "alert": alert}


def _heat(data: dict) -> dict | None:
    otb = (data.get("otb_by_date") or [])[:60]
    rooms = int(data.get("total_rooms") or 0)
    if not otb or not rooms:
        return None

    def bucket(occ):
        # 8b thresholds: <20 / 20-34 / 35-49 / 50-64 / 65-77 / 78-87 / ≥88
        for i, top in enumerate((0.20, 0.35, 0.50, 0.65, 0.78, 0.88)):
            if occ < top:
                return i
        return 6

    cells_raw = []
    for r in otb:
        try:
            d = _dt.strptime(str(r["stay_date"])[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        occ = (r.get("rn_ty") or 0) / rooms
        occ_ly = (r.get("rn_stly") or 0) / rooms
        cells_raw.append({"d": d, "occ": occ,
                          "anomaly": occ_ly >= 0.30 and occ < 0.5 * occ_ly})
    if not cells_raw:
        return None
    # Continuous calendar (user spec 2026-07-30): consecutive boxes, weekday
    # aligned; a month change is marked by a small border on the new month's
    # first cell — no full-width divider row.
    cells = []
    anoms = []
    prev_month = cells_raw[0]["d"].month
    for _ in range(cells_raw[0]["d"].weekday()):
        cells.append({"empty": True})
    for c in cells_raw:
        b = bucket(c["occ"])
        if c["anomaly"]:
            anoms.append(c["d"].strftime("%d/%m"))
        new_month = c["d"].month != prev_month
        prev_month = c["d"].month
        cells.append({
            "empty": False,
            "bg": RAMP[b],
            "ring": c["anomaly"],
            "new_month": new_month,
            "fg": "#FFFFFF" if b >= 4 else "#1D1B20",
            "sub": "rgba(255,255,255,.72)" if b >= 4 else "#79747E",
            "occ_txt": f"{c['occ'] * 100:.0f}%",
            "date_txt": c["d"].strftime("%d/%m"),
        })
    while len(cells) % 7:
        cells.append({"empty": True})
    return {"cells": cells, "ramp": RAMP,
            "alert": ("Dates far behind last year: " + ", ".join(anoms)) if anoms else None}


def _adr_bridge_core(ly: dict, ty: dict, min_share=D("0.03")) -> dict:
    """Reference implementation (adr-bridge-implementation-spec §6).
    Identity guaranteed: sum(mix) + sum(rate) == ADR_ty - ADR_ly."""
    rn_ly_t = sum(v[0] for v in ly.values())
    rn_ty_t = sum(v[0] for v in ty.values())
    if rn_ly_t == 0 or rn_ty_t == 0:
        raise ValueError("zero room nights in a period")
    keep = set()
    for k in set(ly) | set(ty):
        s_ly = ly.get(k, (D(0), D(0)))[0] / rn_ly_t
        s_ty = ty.get(k, (D(0), D(0)))[0] / rn_ty_t
        if max(s_ly, s_ty) >= min_share:
            keep.add(k)

    def fold(d):
        out, orn, orev = {}, D(0), D(0)
        for k, (rn, rev) in d.items():
            if k in keep:
                out[k] = (rn, rev)
            else:
                orn += rn; orev += rev
        if orn > 0:
            out["Other"] = (orn, orev)
        return out

    ly, ty = fold(ly), fold(ty)
    rn_l = sum(v[0] for v in ly.values()); rev_l = sum(v[1] for v in ly.values())
    rn_t = sum(v[0] for v in ty.values()); rev_t = sum(v[1] for v in ty.values())
    adr_l, adr_t = rev_l / rn_l, rev_t / rn_t
    adr_bar = (adr_t + adr_l) / 2
    rows = []
    for k in sorted(set(ly) | set(ty)):
        a_rn, a_rev = ly.get(k, (D(0), D(0)))
        b_rn, b_rev = ty.get(k, (D(0), D(0)))
        w_l, w_t = a_rn / rn_l, b_rn / rn_t
        r_l = (a_rev / a_rn) if a_rn > 0 else (b_rev / b_rn if b_rn > 0 else D(0))
        r_t = (b_rev / b_rn) if b_rn > 0 else r_l
        rate = ((w_t + w_l) / 2) * (r_t - r_l)
        mix = (w_t - w_l) * (((r_t + r_l) / 2) - adr_bar)
        rows.append(dict(channel=k, share_ly=w_l, share_ty=w_t,
                         adr_ly=r_l, adr_ty=r_t, mix=mix, rate=rate))
    tot_mix = sum(r["mix"] for r in rows)
    tot_rate = sum(r["rate"] for r in rows)
    delta = adr_t - adr_l
    return dict(adr_ly=adr_l, adr_ty=adr_t, delta=delta, mix=tot_mix,
                rate=tot_rate, residual=delta - (tot_mix + tot_rate), rows=rows)


def _bridge(data: dict) -> dict | None:
    """ADR bridge card — mix vs rate, consumed MTD vs LY-364d (Q15 input)."""
    src = data.get("consumed_by_source") or []
    ly = {r["source"]: (D(str(r["rn"])), D(str(r["rev"])))
          for r in src if r.get("period") == "LY" and r.get("rn")}
    ty = {r["source"]: (D(str(r["rn"])), D(str(r["rev"])))
          for r in src if r.get("period") == "TY" and r.get("rn")}
    if not ly or not ty:
        return None
    b = _adr_bridge_core(ly, ty)
    if abs(b["residual"]) > D("0.01"):   # spec §9: hard fail, never show
        print(f"[charts] bridge identity broken (residual {b['residual']}) — suppressed")
        return None
    adr_l, adr_t = float(b["adr_ly"]), float(b["adr_ty"])
    mix, rate = float(b["mix"]), float(b["rate"])
    scale = max(adr_l, adr_t, adr_l + max(mix, 0)) * 1.06 or 1

    def X(v):
        return round(max(min(v / scale * 100, 100), 0), 1)

    # four-bar floating bridge (spec §11): LY, mix step, rate step, TY
    mix_left, mix_right = sorted((adr_l, adr_l + mix))
    rate_left, rate_right = sorted((adr_l + mix, adr_t))
    dominant = "mix" if abs(mix) > abs(rate) else "rate"
    top = max(b["rows"], key=lambda r: abs(r["mix"] if dominant == "mix" else r["rate"]))
    yday = _date.today() - _td(days=1)
    both = min(abs(mix), abs(rate)) >= 0.30 * max(abs(float(b["delta"])), 0.01)
    d_word = "below" if b["delta"] < 0 else "above"
    if both:
        sent = (f"{yday.strftime('%B')} ADR is €{abs(float(b['delta'])):.0f} {d_word} last year — "
                f"€{abs(mix):.0f} from mix and €{abs(rate):.0f} from rate. "
                f"{top['channel']} accounts for the largest share.")
    elif dominant == "mix":
        sent = (f"{yday.strftime('%B')} ADR is €{abs(float(b['delta'])):.0f} {d_word} last year. "
                f"€{abs(mix):.0f} of that is mix: {top['channel']} moved from "
                f"{float(top['share_ly'])*100:.0f}% to {float(top['share_ty'])*100:.0f}% of room nights. "
                f"Like-for-like, rate is {'down' if rate < 0 else 'up'} €{abs(rate):.0f}.")
    else:
        sent = (f"{yday.strftime('%B')} ADR is €{abs(float(b['delta'])):.0f} {d_word} last year, "
                f"with €{abs(rate):.0f} of it rate movement at constant mix. "
                f"The largest single contribution is {top['channel']} at "
                f"€{float(top['rate'] if dominant == 'rate' else top['mix']):+.0f}.")
    rows = []
    for r in sorted(b["rows"], key=lambda r: -abs(r["mix"] + r["rate"]))[:5]:
        rows.append({
            "channel": r["channel"],
            "share": f"{float(r['share_ly'])*100:.0f}% → {float(r['share_ty'])*100:.0f}%",
            "adr": f"€{float(r['adr_ly']):.0f} → €{float(r['adr_ty']):.0f}",
            "mix_txt": f"{float(r['mix']):+.0f}",
            "rate_txt": f"{float(r['rate']):+.0f}",
        })
    return {
        "period": f"{yday.strftime('%B')} 1–{yday.day} vs last year (day-of-week aligned)",
        "adr_ly_txt": f"€{adr_l:.0f}", "adr_ty_txt": f"€{adr_t:.0f}",
        "delta_txt": f"{float(b['delta']):+.0f}",
        "mix_txt": f"{mix:+.0f}", "rate_txt": f"{rate:+.0f}",
        "ly_w": X(adr_l), "ty_w": X(adr_t),
        "mix_x": X(mix_left), "mix_w": max(X(mix_right) - X(mix_left), 0.5),
        "rate_x": X(rate_left), "rate_w": max(X(rate_right) - X(rate_left), 0.5),
        "dominant": dominant, "sentence": sent, "rows": rows,
    }


def compute_chart_series(data: dict[str, Any]) -> dict[str, Any]:
    """Never raises — a chart whose data is missing/broken is simply None."""
    out = {}
    for key, fn in (("meter", _meter), ("velocity", _velocity),
                    ("butterfly", _butterfly), ("heat", _heat),
                    ("bridge", _bridge)):
        try:
            out[key] = fn(data)
        except Exception as exc:  # noqa: BLE001 — charts must never block a briefing
            print(f"[charts] {key} series failed (non-blocking): {exc}")
            out[key] = None
    return out
