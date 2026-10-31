"""
Intraday notifications — deterministic, zero-Claude.

ONE ANALYST BEHIND EVERY NOTIFICATION (2026-10-31, user: "yes go for all fixes
now"): the scheduled data-only refreshes (07 / 10 / 13 / 16 / 19 UTC) run FL
Pulse's OWN checks on TODAY-SO-FAR — Signal 1 (bookings, |z| >= 2 vs the prior
7 days, stake floor) and Signal 6 (cancellation spike, z >= 2, >= 3 rn, real
cancelled revenue >= €1,000), from briefing/analyst.py unchanged. The snapshot is
re-anchored so the analyst's 'yesterday' is today's partial day (Q9 / Q14 carry
the run day's bookings). UPWARD events only: a partial day can only under-count,
so a surge or a spike that already clears the gate is real, while a 'slow day'
at 10:00 is not. The old fixed rules (cancellations >= 8 and 3×, a month newly
−5%, a month past last year's final, a 15-room day) are retired — they could
tell a phone something the Pulse never said.

At most one ALERT (cancellation spike) and one good-news push ('momentum',
booking surge) per hotel per day: the `intraday_log` row is claimed BEFORE
sending, so two runs can never double-push. Each claimed event is also added to
the briefing as `ai_insights.since_morning`, shown at the top of FL Pulse — the
push opens the app on the thing it announced. Text is plain words from real
PMS numbers (never derived euro figures).

Also home of `headline()` — the deterministic quiet-day morning-push body (same
rule ladder as the app's Smart Summary — change both together).
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


def cancel_weeks(data: dict[str, Any]) -> tuple[int, int | None]:
    """(cancelled last 7 days, cancelled the 7 days before that) in room nights.
    Last-7 comes from the Pickup card (Q3 `cancellations7d`) so the headline
    reconciles with what the app shows; prior-7 comes from Q14 `cancel_daily`
    (14-day window, all stay dates). Prior is None when Q14 is missing —
    then no comparison is possible and the cancellation rule stays silent."""
    pu = data.get("pickup", {}) or {}
    last7 = int(pu.get("cancellations7d", 0) or 0)
    cd = data.get("cancel_daily", []) or []
    if not cd:
        return last7, None
    today = date.today()
    lo, hi = today - timedelta(days=13), today - timedelta(days=7)
    prior7 = 0
    seen = False
    for r in cd:
        try:
            d = date.fromisoformat(str(r.get("ref_date"))[:10])
        except ValueError:
            continue
        if lo <= d <= hi:
            seen = True
            prior7 += int(r.get("cancel_rn", 0) or 0)
    return last7, (prior7 if seen else None)


def headline(data: dict[str, Any]) -> str:
    """One sentence for the morning push body. Facts only, no estimates.
    Mirror of the app's Smart Summary ladder (keep both in sync):
      1. big yesterday (|vs LY| >= 15%)         -> "Strong/Soft {weekday} — …"
      2. cancellations UP vs the week before    -> "Cancellations up — …"
      3. worst forward month <= -5% vs STLY     -> "{Month} needs attention — …"
      4. else                                   -> "Steady day — MTD …"
    Rule 2 (2026-08-28): compares to the hotel's own recent normal instead of
    an absolute churn ratio — late-season resorts always exceeded 15% churn
    because new bookings dry up while cancellations of old bookings continue,
    so both hotels showed "Watch cancellations" every day."""
    y = data.get("yesterday", {}) or {}
    m = data.get("mtd", {}) or {}
    pu = data.get("pickup", {}) or {}
    booked7 = (pu.get("last7d") or {}).get("roomNights", 0) or 0
    yd_var = _var(y.get("revenue", 0) or 0, y.get("revenueLY", 0) or 0)
    mtd_var = _var(m.get("revenue", 0) or 0, m.get("revenueLY", 0) or 0)

    if abs(yd_var) >= 15:
        try:
            wd = datetime.fromisoformat(str(data.get("report_date"))).strftime("%A")
        except ValueError:
            wd = "day"
        word = "Strong" if yd_var > 0 else "Soft"
        return f"{word} {wd} — {_eur0(y.get('revenue', 0))}, {yd_var:+.0f}% on last year."
    cancelled7, prior7 = cancel_weeks(data)
    total_rooms = int(data.get("total_rooms", 0) or 0)
    floor = max(10, round(total_rooms * 7 * 0.03))          # 3% of a week's capacity
    churn = cancelled7 / booked7 if booked7 > 0 else 0.0
    if (prior7 is not None and cancelled7 >= floor
            and cancelled7 >= 1.5 * max(prior7, 1) and churn >= 0.15):
        return f"Cancellations up — {cancelled7} rooms out this week, vs {prior7} the week before."
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


# ── intraday checks: FL Pulse's own gates on today-so-far (2026-10-31) ───────

def _today_local(tz: str = "Europe/Athens") -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz)).date()


def _fv(facts: dict, key: str) -> str:
    v = facts.get(key)
    return str(v.get("value") if isinstance(v, dict) else (v or ""))


def _plain(v: str) -> str:
    """'+44 rn' → '+44 room nights', '6.7 rn/day' → '6.7 a day' (a phone, not a report)."""
    return str(v).replace(" rn/day", " a day").replace(" rn", " room nights")


def pulse_checks(data: dict[str, Any], today: date | None = None) -> list[dict[str, str]]:
    """Up to one ALERT (cancellation spike) and one good-news event (booking
    surge) for TODAY so far, decided by the analyst's own signal functions —
    same gates, same numbers as the Pulse. Pure: no store, no network.
    Returns [{type, card_id, month, title, body}]."""
    from briefing import analyst as A
    today = today or _today_local()
    # the analyst reads report_date_iso as 'yesterday': point it at today-so-far
    ctx = A._signal_context(dict(data, report_date_iso=today.isoformat()))
    best: dict[str, tuple[float, dict]] = {}
    for kind, fn, prefix in (("alerts", A._signal_cancellations, "cancel_"),
                             ("momentum", A._signal_pickup, "pickup_")):
        try:
            cands = fn(ctx) or []
        except Exception as exc:  # noqa: BLE001 — one broken check never takes the other
            print(f"[intraday] {kind} check failed (non-fatal): {exc}")
            continue
        for c in cands:
            cid = str((c.get("insight") or {}).get("id") or "")
            if not cid.startswith(prefix):
                continue
            if kind == "momentum" and c.get("tag") != "OPPORTUNITY":
                continue                     # a slow partial day is not news
            score = float(c.get("score") or 0)
            if kind not in best or score > best[kind][0]:
                best[kind] = (score, c)
    out: list[dict[str, str]] = []
    for kind, (_, c) in best.items():
        ins = c.get("insight") or {}
        facts = ins.get("facts") or {}
        month = str(facts.get("month_label") or "")
        if kind == "alerts":
            title = f"Cancellations spiking for {month}"
            body = (f"{_plain(_fv(facts, 'cancelled_yday'))} cancelled so far today for {month} "
                    f"({_fv(facts, 'cancelled_rev_yday')} of room revenue), against a normal "
                    f"{_plain(_fv(facts, 'cancel_avg_prior'))}.")
        else:
            title = f"Strong booking day for {month}"
            body = (f"{_plain(_fv(facts, 'yday_net_rn'))} added so far today for {month} "
                    f"({_fv(facts, 'yday_net_rev')}), against a normal "
                    f"{_plain(_fv(facts, 'trailing_avg'))}.")
        out.append({"type": kind, "card_id": str(ins.get("id") or ""), "month": month,
                    "title": title, "body": body})
    return out


# ── send path (claim-then-send; prefs-aware via cloud_push) ──────────────────

def _claim(hotel_id: str, ntype: str) -> bool:
    """Insert the (hotel, day, type) marker; False = already sent or no table.
    With STORAGE=pg the Postgres insert decides (2026-10-28); Supabase gets a
    best-effort echo. A PG error means "do not send" — never a duplicate push."""
    from db import store
    if store.read_from_pg():
        from db import sb_echo
        day = str(date.today())
        won = store.claim_intraday(hotel_id, day, ntype)
        if won:
            sb_echo.echo("POST", "intraday_log", None,
                         {"hotel_id": hotel_id, "day": day, "type": ntype})
            return True
        print(f"[intraday] {ntype} already sent today — skipping." if won is False
              else f"[intraday] log claim error — not sending.")
        return False
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
            try:  # Phase C dual-write mirror (no-op unless STORAGE=dual|pg)
                from db import store
                store.claim_intraday(hotel_id, str(date.today()), ntype)
            except Exception:
                pass
            return True
        if r.status_code == 409 or "duplicate" in r.text:
            print(f"[intraday] {ntype} already sent today — skipping.")
        else:
            print(f"[intraday] log claim failed ({r.status_code}) — not sending. {r.text[:120]}")
        return False
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        print(f"[intraday] log claim error — not sending: {exc}")
        return False


def intraday_events(data: dict[str, Any], hotel_id: str) -> list[dict[str, str]]:
    """Pulse checks on today-so-far + the once-a-day claim per type. Returns
    the CLAIMED events, each stamped with the Athens time it was found — the
    pipeline adds them to the briefing (`since_morning`) BEFORE publishing and
    pushes them after. Fail-open: any error = no event, never a broken run."""
    try:
        hits = pulse_checks(data)
    except Exception as exc:  # noqa: BLE001
        print(f"[intraday] Pulse checks failed (non-fatal): {exc}")
        return []
    if not hits:
        print("[intraday] No Pulse gate fired on today so far.")
        return []
    from zoneinfo import ZoneInfo
    at = datetime.now(ZoneInfo("Europe/Athens")).strftime("%H:%M")
    return [{**h, "at": at} for h in hits if _claim(hotel_id, h["type"])]


def send_intraday(events: list[dict[str, str]], hotel_id: str, hotel_name: str) -> None:
    """Push the claimed events; the tap opens FL Pulse, where `since_morning`
    shows them (quiet hours and per-type choices apply in cloud_push)."""
    from briefing.cloud_push import send_typed_push
    for e in events or []:
        try:
            send_typed_push(hotel_id, hotel_name, e["title"], e["body"], e["type"], section_id="sec-ai")
        except Exception as exc:  # noqa: BLE001
            print(f"[intraday] push failed (non-fatal): {exc}")


def run_intraday_checks(data: dict[str, Any], prev: dict[str, Any] | None,
                        hotel_id: str, hotel_name: str) -> None:
    """Backward-compatible wrapper: claim + push, without the app item."""
    send_intraday(intraday_events(data, hotel_id), hotel_id, hotel_name)
