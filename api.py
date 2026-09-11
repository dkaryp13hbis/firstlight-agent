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
    # 5 intraday data-only refreshes (user-approved 2026-09-10, follow-up
    # engine cadence): ~10:00-22:00 hotel time (Athens = UTC+2/+3). No AI —
    # events, alert checks and watch states only; push volume gated by
    # thresholds, not cadence.
    for _h in (7, 10, 13, 16, 19):
        sched.add_job(lambda: core.run_all_hotels(data_only=True), "cron", hour=_h, minute=0)
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

# The PWA calls the API from the browser (admin portal, 2026-09-11 — first
# browser consumer of these endpoints; everything else reads Supabase direct).
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://firstlight.hbis.io",
        "https://firstlight-pwa.pages.dev",
        "http://localhost:5173",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


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
        _USER_EMAILS[uid] = (r.json().get("email") or "").lower()
        if len(_USERS) > 500:
            _USERS.clear()
            _USER_EMAILS.clear()
    return uid


_USER_EMAILS: dict[str, str] = {}   # uid -> email, filled by auth_user

# Admin gate (v1, pre-C3): superadmin = the founder's emails. Under C3 this
# becomes users.is_superadmin; the endpoint contract stays the same.
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv(
    "ADMIN_EMAILS", "dk@bi-automations.com,d.karypidis@hbis.io").split(",") if e.strip()}


def require_admin(request: Request) -> str:
    uid = auth_user(request)
    if _USER_EMAILS.get(uid, "") not in ADMIN_EMAILS:
        raise HTTPException(403, "admin only")
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
    # The user's own rows PLUS hotel-level FirstLight rows (follow-up engine,
    # 2026-09-10). Schema-tolerant: until the source columns are pasted the
    # or/select 400s and we fall back to the legacy per-user query.
    try:
        rows = _sb_get("watchlist", {
            "hotel_id": f"eq.{hotel_id}",
            "or": f"(user_id.eq.{uid},source.eq.firstlight)",
            "select": "id,kind,key,label,note,created_at,source,flagged_date,first_gap,last_gap",
            "order": "created_at.asc",
        })
    except Exception:
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


@app.get("/admin/usage")
def admin_usage(request: Request):
    """Usage per hotel and per user, last 30 days (superadmin only).
    Reads usage_events with the service role — the app itself can only
    WRITE events (RLS), so this endpoint is the sole read path."""
    require_admin(request)
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    hotels = {h["id"]: h["name"] for h in _sb_get(
        "hotels", {"select": "id,name"})}
    members = _sb_get("hotel_users", {"select": "user_id,hotel_id"})
    events = _sb_get("usage_events", {
        "select": "user_id,hotel_id,event,created_at",
        "created_at": f"gte.{since}",
        "order": "created_at.desc", "limit": "10000",
    })

    # emails via GoTrue admin (service key); fail-open to short ids
    emails: dict[str, str] = {}
    try:
        url, key = _sb()
        r = _req.get(f"{url}/auth/v1/admin/users", params={"per_page": "200"},
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     timeout=10)
        if r.ok:
            for u in (r.json().get("users") or []):
                emails[u["id"]] = u.get("email") or u["id"][:8]
    except Exception:
        pass

    per: dict[tuple, dict] = {}   # (hotel_id, user_id) -> stats
    for m in members:
        per[(m["hotel_id"], m["user_id"])] = {
            "events_30d": 0, "opens_30d": 0, "days": set(), "last_seen": None,
            "top": {},
        }
    for e in events:
        k = (e.get("hotel_id"), e["user_id"])
        if k not in per:
            per[k] = {"events_30d": 0, "opens_30d": 0, "days": set(),
                      "last_seen": None, "top": {}}
        s = per[k]
        s["events_30d"] += 1
        if e["event"] == "app_open":
            s["opens_30d"] += 1
        s["days"].add(e["created_at"][:10])
        s["top"][e["event"]] = s["top"].get(e["event"], 0) + 1
        if s["last_seen"] is None or e["created_at"] > s["last_seen"]:
            s["last_seen"] = e["created_at"]

    out = []
    for hid, name in hotels.items():
        users = []
        for (h, u), s in per.items():
            if h != hid:
                continue
            users.append({
                "user_id": u, "email": emails.get(u, u[:8]),
                "events_30d": s["events_30d"], "opens_30d": s["opens_30d"],
                "days_active": len(s["days"]),
                "last_seen": s["last_seen"],
                "top": sorted(s["top"].items(), key=lambda x: -x[1])[:3],
            })
        users.sort(key=lambda x: x["last_seen"] or "", reverse=True)
        out.append({
            "hotel_id": hid, "name": name,
            "events_30d": sum(x["events_30d"] for x in users),
            "users": users,
        })
    out.sort(key=lambda h: -h["events_30d"])
    return {"since": since[:10], "hotels": out}


@app.get("/admin/clients")
def admin_clients(request: Request):
    """The client portal (superadmin): every hotel with its users, 30-day
    usage, and commercial state (subscriptions table; null until the SQL is
    pasted). One call = the whole book of business."""
    require_admin(request)
    usage = admin_usage.__wrapped__(request) if hasattr(admin_usage, "__wrapped__") \
        else admin_usage(request)   # same aggregation, same gate (cached auth)
    subs: dict[str, dict] = {}
    subs_ready = True
    try:
        for s in _sb_get("subscriptions", {
                "select": "hotel_id,plan,status,price_eur,started_on,renews_on,notes"}):
            subs[s["hotel_id"]] = s
    except HTTPException:
        subs_ready = False   # table not pasted yet
    for h in usage["hotels"]:
        h["subscription"] = subs.get(h["hotel_id"])
    usage["subs_ready"] = subs_ready
    return usage


# ── Superadmin portal sections (ADMIN_PLAN §2/§6/§7/§10; /admin prefix
# until C3 brings is_platform_admin + TOTP — gate contract unchanged) ────────

def _audit_admin(request: Request, action: str, target_type: str | None = None,
                 target_id: str | None = None, before=None, after=None,
                 reason: str | None = None) -> None:
    from db import store
    uid = auth_user(request)
    store.audit(_USER_EMAILS.get(uid, uid), action, target_type, target_id,
                before, after, reason)


@app.get("/admin/audit")
def admin_audit_list(request: Request, action: str = Query(None),
                     target_id: str = Query(None), limit: int = Query(100, le=500)):
    require_admin(request)
    from db import store
    return {"rows": store.audit_list(limit=limit, action=action, target_id=target_id)}


@app.get("/admin/hotels")
def admin_hotels(request: Request):
    require_admin(request)
    from db import store
    hotels = _sb_get("hotels", {
        "select": "id,name,active,total_rooms,pms_type,pms_config,api_token"})
    runs = {r["hotel_id"]: r for r in store.hotels_run_summary(30)}
    briefs = {}
    for b in _sb_get("briefings", {"select": "hotel_id,report_date",
                                   "order": "report_date.desc", "limit": "50"}):
        briefs.setdefault(b["hotel_id"], b["report_date"])
    out = []
    for h in hotels:
        cfg = h.get("pms_config") or {}
        r = runs.get(h["id"], {})
        out.append({
            "id": h["id"], "name": h["name"], "active": h["active"],
            "total_rooms": h.get("total_rooms"),
            "pms_type": h.get("pms_type") or "protel_mssql",
            "fetch_mode": cfg.get("fetch_mode"),
            "tunnel_hostname": cfg.get("tunnel_hostname"),
            "credentials_present": bool((cfg.get("sql") or {}).get("password")),
            "token_present": bool(h.get("api_token")),
            "last_briefing": briefs.get(h["id"]),
            "runs_30d": r.get("runs", 0), "ok_30d": r.get("ok", 0),
            "degraded_30d": r.get("degraded", 0), "failed_30d": r.get("failed", 0),
            "cost_30d_usd": float(r.get("cost_usd") or 0),
            "last_run_at": r.get("last_run_at"), "last_status": r.get("last_status"),
        })
    return {"hotels": out}


@app.get("/admin/hotels/{hotel_id}/runs")
def admin_hotel_runs(hotel_id: str, request: Request):
    require_admin(request)
    from db import store
    return {"runs": store.hotel_runs(hotel_id, 30)}


@app.post("/admin/hotels/{hotel_id}/refresh", status_code=202)
def admin_hotel_refresh(hotel_id: str, request: Request):
    """Manual refresh through the SAME fail-open pipeline (refresh_commands)."""
    require_admin(request)
    _sb_write("POST", "refresh_commands", None,
              {"hotel_id": hotel_id, "type": "manual", "status": "pending"})
    _audit_admin(request, "hotel.refresh", "hotel", hotel_id)
    return {"queued": True}


@app.post("/admin/hotels/{hotel_id}/token/rotate")
def admin_hotel_token(hotel_id: str, request: Request):
    require_admin(request)
    from db import store
    new_token = "flh_" + secrets.token_urlsafe(24)
    _sb_write("PATCH", "hotels", {"id": f"eq.{hotel_id}"}, {"api_token": new_token})
    store.update_hotel_fields(hotel_id, {"api_token": new_token})
    with _TOKENS_LOCK:
        _TOKENS["at"] = 0.0          # bust the 60s token cache
    _audit_admin(request, "hotel.token_rotate", "hotel", hotel_id)
    return {"api_token": new_token}   # shown ONCE in the portal


@app.post("/admin/hotels/{hotel_id}/active")
def admin_hotel_active(hotel_id: str, request: Request, body: dict):
    require_admin(request)
    from db import store
    active = bool(body.get("active"))
    reason = (body.get("reason") or "").strip()
    if not active and not reason:
        raise HTTPException(422, "pausing needs a reason")
    _sb_write("PATCH", "hotels", {"id": f"eq.{hotel_id}"}, {"active": active})
    store.update_hotel_fields(hotel_id, {"active": active})
    _audit_admin(request, "hotel.activate" if active else "hotel.pause",
                 "hotel", hotel_id, reason=reason or None)
    return {"active": active}


@app.get("/admin/health")
def admin_health(request: Request):
    require_admin(request)
    from db import store
    from briefing.audit import audit_all
    try:
        verdict = audit_all()
    except Exception as exc:
        verdict = {"error": str(exc)[:200]}
    return {
        "verdict": verdict,
        "matrix": store.runs_matrix(7),
        "ai": store.ai_stats(14),
        "infra": {
            "db_size_mb": store.db_size_mb(),
            "storage_mode": os.getenv("STORAGE", "supabase"),
            "build": os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:7],
        },
    }


@app.get("/admin/feedback")
def admin_feedback(request: Request, limit: int = Query(200, le=500)):
    require_admin(request)
    rows = _sb_get("insight_feedback", {
        "select": "hotel_id,report_date,card_id,verdict,note,created_at",
        "order": "created_at.desc", "limit": str(limit)})
    hotels = {h["id"]: h["name"] for h in _sb_get("hotels", {"select": "id,name"})}
    for r in rows:
        r["hotel"] = hotels.get(r.get("hotel_id"), "?")
    return {"rows": rows}


@app.put("/admin/subscription/{hotel_id}")
def admin_subscription_put(hotel_id: str, request: Request, body: dict):
    """Upsert a hotel's commercial state (superadmin only)."""
    require_admin(request)
    allowed = {"plan", "status", "price_eur", "started_on", "renews_on", "notes"}
    row = {k: (v if v not in ("", None) else None)
           for k, v in body.items() if k in allowed}
    if row.get("plan") not in (None, "trial", "monthly", "annual"):
        raise HTTPException(422, "plan must be trial|monthly|annual")
    if row.get("status") not in (None, "active", "paused", "cancelled"):
        raise HTTPException(422, "status must be active|paused|cancelled")
    row["hotel_id"] = hotel_id
    from datetime import datetime, timezone
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    out = _sb_write("POST", "subscriptions", {"on_conflict": "hotel_id"}, row,
                    prefer="resolution=merge-duplicates,return=representation")
    return (out or [row])[0]


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
