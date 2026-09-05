"""
Daily data-freshness audit (2026-09-04) — the ops tripwire for "the refresh
didn't run" AND for the sneakier "the refresh ran but the hotel's own SQL
job froze, so the data isn't moving".

Three checks per active hotel, every morning at 07:10 UTC (after the 06:00
catch-up had its chance):

  1. MISSED  — latest briefing report_date < yesterday (no new briefing).
  2. FAILED  — no successful/degraded run today in refresh_runs.
  3. FROZEN  — briefings exist but the data shows zero movement: bookings
               AND cancellations at 0 for the last 2+ days, or yesterday's
               revenue+rooms byte-identical across days (a frozen PMS export
               fingerprint). Skipped when MTD revenue is 0 (closed season).

Problems → ONE email to OPS_EMAIL (default dk@bi-automations.com) via the
existing SMTP config. No problems → a log line only. Everything fail-open:
the audit must never break the scheduler.

`stale_hotels()` feeds GET /health (60s cache) so an external uptime pinger
can alert on staleness even when this process cannot send email.
"""
from __future__ import annotations

import os
import time as _time
from datetime import date, timedelta
from typing import Any

import requests as _req

_CACHE: dict[str, Any] = {"at": 0.0, "stale": []}


def _sb() -> tuple[str, str]:
    return os.getenv("SUPABASE_URL", "").rstrip("/"), os.getenv("SUPABASE_SERVICE_KEY", "")


def _get(path: str, params: dict) -> list:
    url, key = _sb()
    r = _req.get(f"{url}/rest/v1/{path}", params=params,
                 headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=15)
    r.raise_for_status()
    return r.json()


def _hotel_report(hotel: dict) -> list[str]:
    """Problem strings for one hotel (empty = healthy)."""
    hid, name = hotel["id"], hotel["name"]
    problems: list[str] = []
    yesterday = str(date.today() - timedelta(days=1))

    rows = _get("briefings", {
        "hotel_id": f"eq.{hid}",
        "select": "report_date,data",
        "order": "report_date.desc", "limit": "4",
    })
    if not rows:
        return [f"{name}: no briefings stored at all"]

    latest = rows[0]["report_date"]
    if latest < yesterday:
        problems.append(f"{name}: MISSED — latest briefing is for {latest} "
                        f"(expected {yesterday}); the overnight refresh did not publish")

    runs = _get("refresh_runs", {
        "hotel_id": f"eq.{hid}",
        "started_at": f"gte.{date.today()}T00:00:00",
        "select": "status,run_type,started_at,error_type",
        "order": "started_at.desc", "limit": "10",
    })
    ok_today = any(r.get("status") in ("success", "degraded") for r in runs)
    if runs and not ok_today:
        worst = runs[0]
        problems.append(f"{name}: FAILED — {len(runs)} run(s) today, none succeeded "
                        f"(latest: {worst.get('run_type')} {worst.get('status')}"
                        f"{', ' + worst['error_type'] if worst.get('error_type') else ''})")
    elif not runs and latest >= yesterday:
        pass  # briefing fresh but runs not visible — run-log is fail-open, don't alarm

    # FROZEN: data present but not moving (skip in closed season: MTD == 0)
    if latest >= yesterday and len(rows) >= 2:
        recent = rows[:3]
        mtd_rev = float(((recent[0].get("data") or {}).get("mtd") or {}).get("revenue") or 0)
        if mtd_rev > 0:
            def movement(r: dict) -> int:
                pu = (r.get("data") or {}).get("pickup") or {}
                booked = int((pu.get("last1d") or {}).get("roomNights") or 0)
                cancelled = int(pu.get("cancellations1d") or 0)
                return booked + cancelled
            if all(movement(r) == 0 for r in recent[:2]):
                problems.append(f"{name}: FROZEN? — zero bookings AND zero cancellations "
                                f"for {min(len(recent), 2)}+ days in open season; the hotel's "
                                f"internal SQL job / PMS export may have stopped")
            ys = [((r.get("data") or {}).get("yesterday") or {}) for r in recent]
            if (len(ys) >= 2 and ys[0].get("revenue") and
                    ys[0].get("revenue") == ys[1].get("revenue") and
                    ys[0].get("roomNights") == ys[1].get("roomNights")):
                problems.append(f"{name}: FROZEN? — 'yesterday' identical across two report "
                                f"days (€{ys[0]['revenue']:,.0f} / {ys[0]['roomNights']} rn); "
                                f"the PMS may be serving a stale snapshot")
    return problems


def audit_all() -> list[str]:
    hotels = _get("hotels", {"active": "eq.true", "select": "id,name"})
    problems: list[str] = []
    for h in hotels:
        try:
            problems.extend(_hotel_report(h))
        except Exception as exc:  # noqa: BLE001 — one hotel's error is itself a finding
            problems.append(f"{h.get('name', h.get('id'))}: audit check errored ({exc})")
    return problems


def stale_hotels(max_age_s: int = 60) -> list[str]:
    """Hotel names with a stale latest briefing — cached, for GET /health."""
    if _time.time() - _CACHE["at"] < max_age_s:
        return _CACHE["stale"]
    stale: list[str] = []
    try:
        yesterday = str(date.today() - timedelta(days=1))
        for h in _get("hotels", {"active": "eq.true", "select": "id,name"}):
            rows = _get("briefings", {"hotel_id": f"eq.{h['id']}",
                                      "select": "report_date",
                                      "order": "report_date.desc", "limit": "1"})
            if not rows or rows[0]["report_date"] < yesterday:
                stale.append(h["name"])
    except Exception:  # noqa: BLE001 — /health must never 500 over this
        return _CACHE["stale"]
    _CACHE["at"], _CACHE["stale"] = _time.time(), stale
    return stale


def _send_ops_email(problems: list[str]) -> None:
    import smtplib
    from email.mime.text import MIMEText
    import config
    to = os.getenv("OPS_EMAIL", "dk@bi-automations.com")
    body = ("FirstLight daily data audit found problems:\n\n"
            + "\n".join(f"  • {p}" for p in problems)
            + "\n\nCheck refresh_runs and the hotel server / tunnel. "
              "This mail is sent only when something is wrong.")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[FirstLight] data audit: {len(problems)} issue(s)"
    msg["From"] = f"{config.SENDER_NAME} <{config.SMTP_USER}>"
    msg["To"] = to
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(config.SMTP_USER, to, msg.as_string())
    print(f"[audit] Ops email sent to {to} ({len(problems)} issues)")


def run_daily_audit() -> None:
    """Scheduler entry — fail-open."""
    try:
        problems = audit_all()
        if problems:
            print(f"[audit] {len(problems)} problem(s): {problems}")
            try:
                _send_ops_email(problems)
            except Exception as exc:  # noqa: BLE001
                print(f"[audit] ops email failed: {exc}")
        else:
            print("[audit] All hotels fresh and moving.")
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] Audit failed (non-fatal): {exc}")


# ── Phase C: dual-write verification (scheduled 07:20 UTC while STORAGE=dual) ─

def dual_verify() -> list[str]:
    """Compare Postgres against Supabase: per-table row counts and the latest
    briefing per active hotel (report_date + yesterday revenue). Returns
    problem strings; empty = stores agree. Server-side only (DATABASE_URL)."""
    from db import store
    if not store.enabled():
        return []
    problems: list[str] = []
    pg = store.counts()
    for table, pg_n in pg.items():
        try:
            url, key = _sb()
            r = _req.get(f"{url}/rest/v1/{table}", params={"select": "id", "limit": "1"},
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Prefer": "count=exact", "Range": "0-0"}, timeout=15)
            sb_n = int(r.headers.get("Content-Range", "*/0").split("/")[-1])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"dual: count check for {table} errored ({exc})")
            continue
        # PG may be AHEAD only transiently; behind = missed dual-writes
        if pg_n < sb_n:
            problems.append(f"dual: {table} behind — pg={pg_n} supabase={sb_n} "
                            f"(missed dual-writes or app-table drift; re-mirror)")
    for h in _get("hotels", {"active": "eq.true", "select": "id,name"}):
        sb_rows = _get("briefings", {"hotel_id": f"eq.{h['id']}",
                                     "select": "report_date,data",
                                     "order": "report_date.desc", "limit": "1"})
        pg_row = store.get_latest_briefing(h["id"], ["report_date", "data"])
        if not sb_rows or not pg_row:
            problems.append(f"dual: {h['name']} latest briefing missing "
                            f"(sb={bool(sb_rows)} pg={bool(pg_row)})")
            continue
        sb_d, pg_d = sb_rows[0], pg_row
        sb_rev = ((sb_d.get("data") or {}).get("yesterday") or {}).get("revenue")
        pg_rev = ((pg_d.get("data") or {}).get("yesterday") or {}).get("revenue")
        if str(sb_d["report_date"]) != str(pg_d["report_date"]) or sb_rev != pg_rev:
            problems.append(f"dual: {h['name']} latest briefing differs — "
                            f"sb {sb_d['report_date']}/€{sb_rev} vs pg {pg_d['report_date']}/€{pg_rev}")
    return problems


def run_dual_verify() -> None:
    """Scheduler entry — fail-open; ops email only on problems."""
    try:
        problems = dual_verify()
        if problems:
            print(f"[dual-verify] {len(problems)} problem(s): {problems}")
            try:
                _send_ops_email(problems)
            except Exception as exc:  # noqa: BLE001
                print(f"[dual-verify] ops email failed: {exc}")
        else:
            from db import store
            state = "stores agree" if store.enabled() else "PG not participating (skipped)"
            print(f"[dual-verify] {state}.")
    except Exception as exc:  # noqa: BLE001
        print(f"[dual-verify] failed (non-fatal): {exc}")
