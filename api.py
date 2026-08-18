"""
FirstLight API — Phase A (FastAPI consolidation).

One container: this app owns the HTTP surface AND the background machinery
(APScheduler crons + refresh_commands poller), imported unchanged from
railway_main. railway_main.py itself is untouched and still runnable
directly — that is the rollback path (revert the Dockerfile CMD).

RULES
- uvicorn MUST run with exactly 1 worker: N workers = N schedulers =
  duplicate briefings/emails (decision 2026-07-26).
- Every read endpoint requires the per-hotel Bearer token
  (hotels.api_token). No token column provisioned yet -> 503, never open.
"""
from __future__ import annotations

import os
import secrets
import threading
import time as _time
from contextlib import asynccontextmanager

import requests as _req
from fastapi import Depends, FastAPI, HTTPException, Query, Request

import railway_main as core

# ── Background machinery (same jobs as railway_main.__main__) ────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler()
    sched.add_job(core.run_all_hotels, "cron", hour=3, minute=30)   # full
    sched.add_job(core.run_all_hotels, "cron", hour=6, minute=0)    # catch-up
    sched.add_job(lambda: core.run_all_hotels(data_only=True), "cron", hour=11, minute=0)
    sched.add_job(lambda: core.run_all_hotels(data_only=True), "cron", hour=17, minute=0)
    # Demo mirror: refresh anonymized copies after the morning run + evening
    from briefing.demo_sync import sync_demo_briefings
    sched.add_job(sync_demo_briefings, "cron", hour=4, minute=15)
    sched.add_job(sync_demo_briefings, "cron", hour=17, minute=20)
    sched.start()
    threading.Thread(target=core._poll_refresh_commands, daemon=True).start()
    core.log.info("[api] Scheduler 03:30 full | 06:00 catch-up | 11:00+17:00 data-only UTC; poller up")
    try:
        core.log.info(f"[api] Hotels: {[h['name'] for h in core._get_hotels()]}")
    except Exception as exc:  # noqa: BLE001 — never block startup on a listing
        core.log.warning(f"[api] Hotel listing failed at startup: {exc}")
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="FirstLight API", version="phase-a", lifespan=lifespan)


# ── Supabase helpers ─────────────────────────────────────────────────────────

def _sb() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(503, "storage not configured")
    return url, key


def _sb_get(path: str, params: dict) -> list:
    url, key = _sb()
    try:
        r = _req.get(f"{url}/rest/v1/{path}", params=params,
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=15)
    except _req.RequestException as exc:
        raise HTTPException(503, "storage unreachable") from exc
    if r.status_code >= 400:
        raise HTTPException(502, f"storage error {r.status_code}")
    return r.json()


# ── Per-hotel token auth (hotels.api_token) ──────────────────────────────────

_TOKENS: dict = {"at": 0.0, "map": {}}          # token -> hotel_id, 60s cache
_TOKENS_LOCK = threading.Lock()


def _token_map() -> dict[str, str]:
    with _TOKENS_LOCK:
        if _time.time() - _TOKENS["at"] < 60:
            return _TOKENS["map"]
    url, key = _sb()
    try:
        r = _req.get(f"{url}/rest/v1/hotels",
                     params={"select": "id,api_token", "active": "eq.true"},
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=10)
    except _req.RequestException as exc:
        raise HTTPException(503, "token store unreachable") from exc
    if r.status_code >= 400:
        # api_token column not provisioned yet — auth stays closed
        raise HTTPException(503, "API tokens not provisioned (hotels.api_token missing)")
    mp = {row["api_token"]: row["id"] for row in r.json() if row.get("api_token")}
    with _TOKENS_LOCK:
        _TOKENS["at"], _TOKENS["map"] = _time.time(), mp
    return mp


def auth_hotel(request: Request, hotel_id: str = Query(...)) -> str:
    """Bearer token must belong to the hotel being queried."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(401, "missing bearer token")
    mapped = _token_map().get(token)
    if not mapped or not secrets.compare_digest(mapped, hotel_id):
        raise HTTPException(403, "token does not match hotel")
    return hotel_id


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "firstlight-api", "phase": "A"}


@app.post("/trigger", status_code=202)
@app.get("/trigger", status_code=202)   # legacy GET compatibility
def trigger(hotel_id: str = Depends(auth_hotel),
            data_only: bool = Query(True)):
    """Queue a refresh for one hotel. Manual semantics: silent, AI reused."""
    threading.Thread(
        target=core.run_all_hotels,
        kwargs={"hotel_id_filter": hotel_id, "force": True,
                "data_only": data_only, "manual": True},
        daemon=True,
    ).start()
    return {"status": "triggered", "hotel_id": hotel_id}


@app.get("/briefing/latest")
def briefing_latest(hotel_id: str = Depends(auth_hotel),
                    include_html: bool = Query(False)):
    cols = "report_date,generated_at,data,ai_insights" + (",rendered_html" if include_html else "")
    rows = _sb_get("briefings", {
        "hotel_id": f"eq.{hotel_id}", "select": cols,
        "order": "report_date.desc", "limit": "1",
    })
    if not rows:
        raise HTTPException(404, "no briefing yet")
    return rows[0]


@app.get("/briefing/history")
def briefing_history(hotel_id: str = Depends(auth_hotel),
                     days: int = Query(7, ge=1, le=60)):
    """Per-day KPI summaries, newest first. kpi_summary is populated at
    publish time from this release on; older rows return null."""
    try:
        rows = _sb_get("briefings", {
            "hotel_id": f"eq.{hotel_id}", "select": "report_date,kpi_summary",
            "order": "report_date.desc", "limit": str(days),
        })
    except HTTPException:
        # kpi_summary column not provisioned yet — dates only, never a 500
        rows = _sb_get("briefings", {
            "hotel_id": f"eq.{hotel_id}", "select": "report_date",
            "order": "report_date.desc", "limit": str(days),
        })
    return {"hotel_id": hotel_id, "days": len(rows), "history": rows}


@app.get("/feedback")
def feedback(hotel_id: str = Depends(auth_hotel),
             days: int = Query(30, ge=1, le=365)):
    """Read the 👍/👎 loop (verdict + note + rated card snapshot)."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = _sb_get("insight_feedback", {
        "hotel_id": f"eq.{hotel_id}",
        "report_date": f"gte.{since}",
        "select": "report_date,card_id,verdict,reason,card_content,created_at",
        "order": "created_at.desc",
    })
    return {"hotel_id": hotel_id, "count": len(rows), "feedback": rows}


@app.post("/push/test")
def push_test(hotel_id: str = Depends(auth_hotel)):
    """Send a test Web Push to every subscription of this hotel — the way to
    verify a phone right after tapping the bell (no need to wait for 03:30).
    Returns how many subscriptions the hotel has and how many sends succeeded."""
    import io, contextlib
    from briefing.cloud_push import _send_push_notifications
    hotels = _sb_get("hotels", {"id": f"eq.{hotel_id}", "select": "name"})
    name = (hotels[0].get("name") if hotels else None) or "FirstLight"
    subs = _sb_get("push_subscriptions", {"hotel_id": f"eq.{hotel_id}", "select": "id"})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _send_push_notifications(
            {"insights": [], "executive_summary":
             "Test notification — push is working on this device. "
             "Your real briefing arrives every morning after the 03:30 UTC run."},
            hotel_id, hotel_name=name)
    log = buf.getvalue().strip().splitlines()
    return {"hotel_id": hotel_id, "subscriptions": len(subs), "log": log[-6:]}


if __name__ == "__main__":
    # Railway-safe entry: `python api.py` — no shell expansion needed.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
