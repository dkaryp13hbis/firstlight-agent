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
    # Daily data-freshness audit (ops email only when something is wrong)
    from briefing.audit import run_daily_audit, run_dual_verify
    sched.add_job(run_daily_audit, "cron", hour=7, minute=10)
    # Phase C: Postgres-vs-Supabase agreement check (no-op unless STORAGE set)
    sched.add_job(run_dual_verify, "cron", hour=7, minute=20)
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


def _sb_write(method: str, path: str, params: dict | None, body,
              prefer: str = "return=minimal") -> list | None:
    url, key = _sb()
    try:
        r = _req.request(method, f"{url}/rest/v1/{path}", params=params or {},
                         json=body,
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json", "Prefer": prefer},
                         timeout=15)
    except _req.RequestException as exc:
        raise HTTPException(503, "storage unreachable") from exc
    if r.status_code == 409 or "23505" in r.text[:200]:
        raise HTTPException(409, "duplicate")
    if r.status_code >= 400:
        raise HTTPException(502, f"storage error {r.status_code}")
    try:
        return r.json() if r.text else None
    except ValueError:
        return None


# ── App-user auth (Supabase JWT; storage-agnostic — survives Phase C) ────────

_USERS: dict = {}                     # jwt -> (user_id, expiry)
_USERS_LOCK = threading.Lock()


def auth_user(request: Request) -> str:
    """The app's Supabase session JWT → user id (verified against GoTrue).
    Auth stays on Supabase through Phase C; only data storage moves."""
    auth = request.headers.get("authorization", "")
    jwt = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not jwt:
        raise HTTPException(401, "missing user token")
    now = _time.time()
    with _USERS_LOCK:
        hit = _USERS.get(jwt)
        if hit and hit[1] > now:
            return hit[0]
    url, key = _sb()
    try:
        r = _req.get(f"{url}/auth/v1/user",
                     headers={"apikey": key, "Authorization": f"Bearer {jwt}"},
                     timeout=10)
    except _req.RequestException as exc:
        raise HTTPException(503, "auth unreachable") from exc
    if r.status_code != 200 or not r.json().get("id"):
        raise HTTPException(401, "invalid user token")
    uid = r.json()["id"]
    with _USERS_LOCK:
        _USERS[jwt] = (uid, now + 300)
        if len(_USERS) > 500:
            _USERS.clear()
    return uid


def require_member(user_id: str, hotel_id: str) -> None:
    rows = _sb_get("hotel_users", {"user_id": f"eq.{user_id}",
                                   "hotel_id": f"eq.{hotel_id}", "select": "id"})
    if not rows:
        raise HTTPException(403, "not a member of this hotel")


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
    from briefing.analyst import _PROMPT_VERSION
    from briefing.audit import stale_hotels
    return {"status": "ok", "service": "firstlight-api", "phase": "A",
            "prompt_version": _PROMPT_VERSION,
            "build": os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:7] or "local",
            # stale = latest briefing older than yesterday; an external uptime
            # pinger can alert on this field even when we can't send email
            "stale_hotels": stale_hotels()}


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


# ── C2 endpoints (Phase C prep 3/3, 2026-09-04): the app's direct Supabase
# reads/writes as API calls. Backed by Supabase today; the storage flip later
# happens inside these handlers only. Hotel-scoped data = hotel token;
# user-owned data = the app's Supabase JWT + hotel membership.

@app.get("/briefing/by-date")
def briefing_by_date(hotel_id: str = Depends(auth_hotel), date: str = Query(...)):
    rows = _sb_get("briefings", {
        "hotel_id": f"eq.{hotel_id}", "report_date": f"eq.{date}",
        "select": "report_date,generated_at,data,ai_insights",
        "order": "generated_at.desc", "limit": "1",
    })
    if not rows:
        raise HTTPException(404, "no briefing for that date")
    return rows[0]


@app.get("/runs")
def runs(hotel_id: str = Depends(auth_hotel), days: int = Query(3, ge=1, le=14)):
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _sb_get("refresh_runs", {
        "hotel_id": f"eq.{hotel_id}", "started_at": f"gte.{since}",
        "select": "started_at,completed_at,run_type,status,error_type,attempt",
        "order": "started_at.desc", "limit": "50",
    })
    return {"hotel_id": hotel_id, "runs": rows}


@app.get("/watchlist")
def watchlist_get(request: Request, hotel_id: str = Query(...)):
    uid = auth_user(request)
    require_member(uid, hotel_id)
    rows = _sb_get("watchlist", {
        "user_id": f"eq.{uid}", "hotel_id": f"eq.{hotel_id}",
        "select": "id,kind,key,label,note,created_at", "order": "created_at.asc",
    })
    return {"items": rows}


@app.post("/watchlist", status_code=201)
def watchlist_add(request: Request, body: dict):
    uid = auth_user(request)
    hotel_id = str(body.get("hotel_id", ""))
    require_member(uid, hotel_id)
    if body.get("kind") not in ("month", "range") or not body.get("key"):
        raise HTTPException(422, "kind must be month|range with a key")
    existing = _sb_get("watchlist", {"user_id": f"eq.{uid}",
                                     "hotel_id": f"eq.{hotel_id}", "select": "id"})
    if len(existing) >= 5:
        raise HTTPException(409, "watchlist is full (5)")
    row = {"user_id": uid, "hotel_id": hotel_id, "kind": body["kind"],
           "key": str(body["key"]), "label": body.get("label")}
    out = _sb_write("POST", "watchlist", None, row, prefer="return=representation")
    return out[0] if out else row


@app.delete("/watchlist/{item_id}")
def watchlist_remove(item_id: str, request: Request):
    uid = auth_user(request)
    _sb_write("DELETE", "watchlist",
              {"id": f"eq.{item_id}", "user_id": f"eq.{uid}"}, None)
    return {"removed": item_id}


@app.post("/feedback", status_code=201)
def feedback_post(request: Request, body: dict):
    uid = auth_user(request)
    hotel_id = str(body.get("hotel_id", ""))
    require_member(uid, hotel_id)
    if body.get("verdict") not in (1, -1) or not body.get("card_id"):
        raise HTTPException(422, "verdict 1|-1 and card_id required")
    row = {"hotel_id": hotel_id, "report_date": body.get("report_date"),
           "card_id": body["card_id"], "verdict": body["verdict"],
           "reason": body.get("reason"), "card_content": body.get("card_content"),
           "user_id": uid}
    _sb_write("POST", "insight_feedback",
              {"on_conflict": "hotel_id,report_date,card_id,user_id"}, row,
              prefer="resolution=merge-duplicates,return=minimal")
    return {"ok": True}


@app.get("/prefs")
def prefs_get(request: Request, hotel_id: str = Query(...)):
    uid = auth_user(request)
    require_member(uid, hotel_id)
    rows = _sb_get("hotel_prefs", {"hotel_id": f"eq.{hotel_id}", "select": "language,updated_at"})
    return rows[0] if rows else {"language": "en"}


@app.put("/prefs")
def prefs_put(request: Request, body: dict):
    uid = auth_user(request)
    hotel_id = str(body.get("hotel_id", ""))
    require_member(uid, hotel_id)
    if body.get("language") not in ("en", "el"):
        raise HTTPException(422, "language must be en|el")
    from datetime import datetime, timezone
    _sb_write("POST", "hotel_prefs", {"on_conflict": "hotel_id"},
              {"hotel_id": hotel_id, "language": body["language"],
               "updated_at": datetime.now(timezone.utc).isoformat()},
              prefer="resolution=merge-duplicates,return=minimal")
    return {"ok": True}


@app.post("/push/subscribe", status_code=201)
def push_subscribe(request: Request, body: dict):
    uid = auth_user(request)
    hotel_id = str(body.get("hotel_id", ""))
    require_member(uid, hotel_id)
    if not isinstance(body.get("subscription"), dict):
        raise HTTPException(422, "subscription object required")
    _sb_write("DELETE", "push_subscriptions",
              {"user_id": f"eq.{uid}", "hotel_id": f"eq.{hotel_id}"}, None)
    _sb_write("POST", "push_subscriptions", None,
              {"hotel_id": hotel_id, "user_id": uid, "subscription": body["subscription"]})
    return {"ok": True}


@app.post("/push/unsubscribe")
def push_unsubscribe(request: Request, body: dict):
    uid = auth_user(request)
    hotel_id = str(body.get("hotel_id", ""))
    _sb_write("DELETE", "push_subscriptions",
              {"user_id": f"eq.{uid}", "hotel_id": f"eq.{hotel_id}"}, None)
    return {"ok": True}


@app.put("/push/prefs")
def push_prefs(request: Request, body: dict):
    uid = auth_user(request)
    hotel_id = str(body.get("hotel_id", ""))
    prefs = body.get("notification_prefs")
    if not isinstance(prefs, dict):
        raise HTTPException(422, "notification_prefs object required")
    _sb_write("PATCH", "push_subscriptions",
              {"user_id": f"eq.{uid}", "hotel_id": f"eq.{hotel_id}"},
              {"notification_prefs": prefs})
    return {"ok": True}


@app.post("/events", status_code=202)
def events(request: Request, body: dict):
    """Usage-tracking batch. user_id is taken from the verified JWT, never
    from the payload."""
    uid = auth_user(request)
    evs = body.get("events")
    if not isinstance(evs, list) or not evs or len(evs) > 100:
        raise HTTPException(422, "events: list of 1..100")
    rows = []
    for e in evs:
        if not isinstance(e, dict) or not e.get("event"):
            continue
        rows.append({"user_id": uid, "hotel_id": e.get("hotel_id"),
                     "session_id": str(e.get("session_id", ""))[:64],
                     "event": str(e["event"])[:64], "props": e.get("props")})
    if rows:
        _sb_write("POST", "usage_events", None, rows)
    return {"accepted": len(rows)}


if __name__ == "__main__":
    # Railway-safe entry: `python api.py` — no shell expansion needed.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
