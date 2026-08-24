"""
Intraday notifications — deterministic, zero-Claude (2026-08-24).

Runs after the scheduled data-only refreshes (11:00 / 17:00 UTC): compares the
fresh snapshot against the previously PUBLISHED one and fires at most one
ALERT and one MOMENTUM push per hotel per day. All text is templated from
real PMS numbers and percentages — never derived euro figures (standing rule).

Dedupe/caps live in the `intraday_log` table (PK hotel_id+day+type): the row
is claimed BEFORE sending, so two runs can never double-push. If the table
does not exist yet (SQL not pasted), checks run but nothing is sent.

Also home of `headline()` — the deterministic morning-push body (same rule
ladder as the app's Smart Summary).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any

import requests as _req


# ── deterministic headline (mirror of the app's Smart Summary ladder) ────────

def _var(now: float, ref: float) -> float:
    return (now - ref) / ref * 100 if ref else 0.0


def _eur0(v: float) -> str:
    return "€" + f"{v:,.0f}".replace(",", ".")


def headline(data: dict[str, Any]) -> str:
    """One sentence for the morning push body. Facts only, no estimates."""
    y = data.get("yesterday", {}) or {}
    m = data.get("mtd", {}) or {}
    pu = data.get("pickup", {}) or {}
    booked7 = (pu.get("last7d") or {}).get("roomNights", 0) or 0
    cancelled7 = pu.get("cancellations7d", 0) or 0
    yd_var = _var(y.get("revenue", 0) or 0, y.get("revenueLY", 0) or 0)
    mtd_var = _var(m.get("revenue", 0) or 0, m.get("revenueLY", 0) or 0)

    if booked7 > 0 and cancelled7 >= 10 and cancelled7 / booked7 >= 0.15:
        return f"Watch cancellations — {cancelled7} rooms out this week."
    if abs(yd_var) >= 15:
        try:
            wd = datetime.fromisoformat(str(data.get("report_date"))).strftime("%A")
        except ValueError:
            wd = "day"
        word = "Strong" if yd_var > 0 else "Soft"
        return f"{word} {wd} — {_eur0(y.get('revenue', 0))}, {yd_var:+.0f}% on last year."
    worst = None
    cur_m = date.today().month
    for p in data.get("pace", []) or []:
        if p.get("month_num", 0) >= cur_m and (p.get("rev_stly") or 0) > 0:
            v = _var(p.get("rev", 0) or 0, p["rev_stly"])
            if worst is None or v < worst[1]:
                worst = (p.get("month", "?"), v)
    if worst and worst[1] <= -5:
        return f"{worst[0]} needs attention — {worst[1]:.0f}% behind last year."
    return f"Steady day — MTD {mtd_var:+.0f}% on last year."


# ── intraday checks (fresh snapshot vs previously published one) ─────────────

def _month_vars(data: dict[str, Any]) -> dict[int, tuple[str, float, float, float]]:
    """month_num -> (name, var vs STLY %, rev, rev_final)."""
    out = {}
    for p in data.get("pace", []) or []:
        if (p.get("rev_stly") or 0) > 0:
            out[p.get("month_num", 0)] = (
                p.get("month", "?"), _var(p.get("rev", 0) or 0, p["rev_stly"]),
                p.get("rev", 0) or 0, p.get("rev_final", 0) or 0)
    return out


def check_intraday(data: dict[str, Any], prev: dict[str, Any] | None) -> list[dict[str, str]]:
    """Return up to one alert and one momentum: [{type,title,body}]. Pure."""
    out: list[dict[str, str]] = []
    cur_m = date.today().month
    now_m = _month_vars(data)
    prev_m = _month_vars(prev) if prev else {}

    # ALERT 1: cancellation spike today (vs trailing daily cancel average)
    pu = data.get("pickup", {}) or {}
    c_today = pu.get("cancellationsToday", 0) or 0
    cd = data.get("cancel_daily", []) or []
    if cd and c_today:
        days = {}
        for r in cd:
            days.setdefault(r.get("ref_date"), 0)
            days[r.get("ref_date")] += r.get("cancel_rn", 0) or 0
        past = sorted(days.items())[:-1][-7:]          # exclude today
        avg = (sum(v for _, v in past) / len(past)) if past else 0
        if c_today >= max(8, 3 * avg):
            out.append({"type": "alerts",
                        "title": "Cancellations spiking today",
                        "body": f"{c_today} rooms cancelled so far today, vs ~{avg:.0f}/day recently. Worth a look before end of day."})

    # ALERT 2: a forward month slipped behind LY since the last snapshot
    if not any(o["type"] == "alerts" for o in out):
        for mn in sorted(now_m):
            if mn < cur_m:
                continue
            name, v_now, _, _ = now_m[mn]
            v_prev = prev_m.get(mn, (None, 99, 0, 0))[1]
            if v_now <= -5 and v_prev > -5:
                out.append({"type": "alerts",
                            "title": f"{name} slipped behind last year",
                            "body": f"{name} OTB is now {v_now:.0f}% behind same time last year (was {v_prev:+.0f}% this morning)."})
                break

    # MOMENTUM 1: a month crossed above last year's FINAL result
    for mn in sorted(now_m):
        name, _, rev, fin = now_m[mn]
        if fin > 0 and rev >= fin:
            p_rev, p_fin = prev_m.get(mn, (None, 0, 0, 0))[2:4]
            if not (p_fin > 0 and p_rev >= p_fin):     # newly crossed
                out.append({"type": "momentum",
                            "title": f"{name} beats last year — already",
                            "body": f"{name} on the books has passed last year's FINAL result, with the month still selling."})
                break

    # MOMENTUM 2: strong booking day so far
    if not any(o["type"] == "momentum" for o in out):
        t_rn = (pu.get("today") or {}).get("roomNights", 0) or 0
        pd = data.get("pickup_daily", []) or []
        if pd and t_rn:
            days = {}
            for r in pd:
                days.setdefault(r.get("ref_date"), 0)
                days[r.get("ref_date")] += r.get("net_rn", 0) or 0
            past = sorted(days.items())[:-1][-7:]
            avg = (sum(v for _, v in past) / len(past)) if past else 0
            if avg > 0 and t_rn >= max(15, 2 * avg):
                out.append({"type": "momentum",
                            "title": "Strong booking day",
                            "body": f"+{t_rn} rooms booked so far today — about double the recent daily pace."})
    return out


# ── send path (claim-then-send; prefs-aware via cloud_push) ──────────────────

def _claim(hotel_id: str, ntype: str) -> bool:
    """Insert the (hotel, day, type) marker; False = already sent or no table."""
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return False
    try:
        r = _req.post(f"{url}/rest/v1/intraday_log",
                      json={"hotel_id": hotel_id, "day": str(date.today()), "type": ntype},
                      headers={"apikey": key, "Authorization": f"Bearer {key}",
                               "Content-Type": "application/json", "Prefer": "return=minimal"},
                      timeout=10)
        if r.status_code in (200, 201):
            return True
        if r.status_code == 409 or "duplicate" in r.text:
            print(f"[intraday] {ntype} already sent today — skipping.")
        else:
            print(f"[intraday] log claim failed ({r.status_code}) — not sending. {r.text[:120]}")
        return False
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        print(f"[intraday] log claim error — not sending: {exc}")
        return False


def run_intraday_checks(data: dict[str, Any], prev: dict[str, Any] | None,
                        hotel_id: str, hotel_name: str) -> None:
    """Fail-open wrapper the pipeline calls after a scheduled data-only publish."""
    try:
        hits = check_intraday(data, prev)
        if not hits:
            print("[intraday] No alert/momentum gates fired.")
            return
        from briefing.cloud_push import send_typed_push
        for h in hits:
            if _claim(hotel_id, h["type"]):
                send_typed_push(hotel_id, hotel_name, h["title"], h["body"], h["type"],
                                section_id="sec-ai" if h["type"] == "alerts" else "sec-overview")
    except Exception as exc:  # noqa: BLE001
        print(f"[intraday] Checks failed (non-fatal): {exc}")
