"""
Railway cloud processor entry point.
- Serves HTTP health check on $PORT (required by Railway)
- Runs daily hotel processing via APScheduler
- Fetches data from hotel bridges (via Cloudflare Tunnel) instead of direct SQL

Schedule (all times UTC, Greece is UTC+3 in summer):
  06:00 UTC = 09:00 Greece — full briefing (AI insights + email + push)
  11:00 UTC = 14:00 Greece — data-only refresh (reuse morning AI, push only)
  17:00 UTC = 20:00 Greece — data-only refresh (reuse morning AI, push only)
"""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("railway")

# ── Concurrency controls (Step 5) ─────────────────────────────────────────────
# REFRESH_CONCURRENCY: how many hotels may process at once. Default 1 because
# config.HOTEL_NAME/TOTAL_ROOMS are process-wide globals mutated per hotel —
# raise only after that state is passed per-call (open item for scale-up).
_refresh_sem = threading.BoundedSemaphore(int(os.getenv("REFRESH_CONCURRENCY", "1")))

# Per-hotel single-flight: a scheduled run and a manual refresh can never
# process the same hotel simultaneously — the second is skipped.
_hotel_locks: dict[str, threading.Lock] = {}
_hotel_locks_guard = threading.Lock()

# Staged timeouts per hotel run (reviewer spec: soft 2m expected, warn 3m, hard 8m)
_SOFT_WARN_S    = int(os.getenv("RUN_WARN_S", "180"))
_HARD_TIMEOUT_S = int(os.getenv("RUN_HARD_TIMEOUT_S", "480"))

# Retry ladder for failed runs: attempt 2 after 5 min, 3 after 15, 4 after 45
_RETRY_DELAYS_S = {2: 300, 3: 900, 4: 2700}


def _get_hotel_lock(hotel_id: str) -> threading.Lock:
    with _hotel_locks_guard:
        return _hotel_locks.setdefault(hotel_id, threading.Lock())


def _schedule_retry(hotel_id: str, data_only: bool, next_attempt: int,
                    manual: bool = False) -> None:
    delay = _RETRY_DELAYS_S.get(next_attempt)
    if delay is None:
        log.error(f"[retry] Giving up on hotel {hotel_id[:8]}… after {next_attempt - 1} attempts.")
        return
    log.info(f"[retry] Scheduling attempt {next_attempt} for hotel {hotel_id[:8]}… in {delay}s")
    t = threading.Timer(delay, _retry_run, args=(hotel_id, data_only, next_attempt, manual))
    t.daemon = True
    t.start()


def _retry_run(hotel_id: str, data_only: bool, attempt: int, manual: bool = False) -> None:
    hotels = [h for h in _get_hotels() if h["id"] == hotel_id]
    if not hotels:
        log.warning(f"[retry] Hotel {hotel_id[:8]}… no longer active — retry dropped.")
        return
    status = process_hotel(hotels[0], force=False, data_only=data_only, attempt=attempt,
                           manual=manual)
    if status == "failed":
        _schedule_retry(hotel_id, data_only, attempt + 1, manual)


_HOTEL_COLS    = "id,name,total_rooms,bridge_url,bridge_secret,recipient_email,recipient_name"
_HOTEL_COLS_V2 = _HOTEL_COLS + ",pms_type,pms_config"


def _get_hotels() -> list[dict]:
    """Load active hotel configs from Supabase. Tries the tunnel-era columns
    (pms_type/pms_config) first and falls back to the legacy column set if the
    migration SQL hasn't run yet — briefings must never stop over a schema gap."""
    import requests as _req
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        log.error("[hotels] SUPABASE_URL or SUPABASE_SERVICE_KEY not set")
        return []
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    for cols in (_HOTEL_COLS_V2, _HOTEL_COLS):
        try:
            resp = _req.get(
                f"{supabase_url}/rest/v1/hotels",
                params={"active": "eq.true", "select": cols},
                headers=headers, timeout=10,
            )
            if resp.status_code == 400 and cols == _HOTEL_COLS_V2:
                log.warning("[hotels] pms_type/pms_config columns missing — using legacy column set.")
                continue
            resp.raise_for_status()
            hotels = resp.json()
            log.info(f"[hotels] Loaded {len(hotels)} active hotels from Supabase")
            return hotels
        except Exception as exc:
            log.error(f"[hotels] Failed to load from Supabase ({cols.split(',')[-1]}): {exc}")
    return []


def _fetch_via_tunnel(hotel: dict, pms_cfg: dict) -> dict:
    """Tunnel-direct fetch: open a cloudflared Access client to the hotel's SQL
    port and run the PMS adapter's queries from the cloud."""
    from db.tunnel import manager
    from db.connection import connect_mssql
    from db.adapters.base import get_adapter

    sql = pms_cfg.get("sql") or {}
    adapter = get_adapter(hotel.get("pms_type") or "protel_mssql")
    with manager.acquire(
        pms_cfg["tunnel_hostname"],
        pms_cfg.get("cf_access_client_id", ""),
        pms_cfg.get("cf_access_client_secret", ""),
    ) as port:
        conn = connect_mssql("127.0.0.1", port,
                             sql["user"], sql["password"],
                             sql.get("database", "bidata"))
        try:
            return adapter.fetch_snapshot(conn, {
                "hotel_name":   hotel["name"],
                "total_rooms":  hotel["total_rooms"],
                "pms_hotel_id": sql.get("pms_hotel_id", 1),
            })
        finally:
            conn.close()


def _fetch_hotel_data(hotel: dict, run) -> dict:
    """Fetch mode switch: tunnel-direct when configured, with automatic
    fallback to the hotel bridge on any tunnel failure (pilot safety net)."""
    from briefing.bridge_fetcher import fetch_from_bridge
    pms_cfg = hotel.get("pms_config") or {}
    if pms_cfg.get("fetch_mode") == "tunnel":
        try:
            data = _fetch_via_tunnel(hotel, pms_cfg)
            run.record(fetch_path="tunnel")
            return data
        except Exception as exc:
            log.warning(f"[processor] {hotel['name']} — tunnel fetch failed "
                        f"({type(exc).__name__}: {exc}); falling back to bridge.")
            run.record(fetch_path="tunnel_failed_bridge_fallback",
                       tunnel_error=f"{type(exc).__name__}: {str(exc)[:300]}")
    else:
        run.record(fetch_path="bridge")
    return fetch_from_bridge(hotel["bridge_url"], hotel["bridge_secret"])


def _briefing_exists_today(hotel_id: str) -> bool:
    """Returns True if a briefing for yesterday already exists in Supabase."""
    import requests as _req
    from datetime import date, timedelta
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    yesterday = str(date.today() - timedelta(days=1))
    try:
        r = _req.get(
            f"{supabase_url}/rest/v1/briefings",
            params={"hotel_id": f"eq.{hotel_id}", "report_date": f"eq.{yesterday}", "select": "id"},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        return r.ok and len(r.json()) > 0
    except Exception:
        return False


def _get_existing_ai_insights(hotel_id: str) -> dict | None:
    """Fetch the AI insights saved by this morning's full briefing run."""
    import requests as _req
    from datetime import date, timedelta
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    yesterday = str(date.today() - timedelta(days=1))
    try:
        r = _req.get(
            f"{supabase_url}/rest/v1/briefings",
            params={"hotel_id": f"eq.{hotel_id}", "report_date": f"eq.{yesterday}", "select": "ai_insights"},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        if r.ok and r.json():
            return r.json()[0].get("ai_insights")
    except Exception:
        pass
    return None


def process_hotel(hotel: dict, force: bool = False, data_only: bool = False,
                  attempt: int = 1, manual: bool = False) -> str:
    """Process one hotel. Returns final status: success | degraded | failed | skipped.
    manual=True (app refresh / trigger): updates the app silently — no email, no push."""
    log.info(f"[processor] Starting: {hotel['name']} (data_only={data_only}, "
             f"attempt={attempt}, manual={manual})")

    # Full briefing: skip if already done today (unless forced)
    if not data_only and not force and _briefing_exists_today(hotel["id"]):
        log.info(f"[processor] Skipped {hotel['name']} — briefing for today already exists.")
        return "skipped"

    # Per-hotel single-flight: never two refreshes for the same hotel at once
    lock = _get_hotel_lock(hotel["id"])
    if not lock.acquire(blocking=False):
        log.info(f"[processor] Skipped {hotel['name']} — a refresh is already running.")
        return "skipped"
    try:
        with _refresh_sem:
            return _run_with_timeout(hotel, force, data_only, attempt, manual)
    finally:
        lock.release()


def _run_with_timeout(hotel: dict, force: bool, data_only: bool, attempt: int,
                      manual: bool = False) -> str:
    """Run the pipeline in a worker thread with staged timeouts: warn at
    _SOFT_WARN_S, abandon at _HARD_TIMEOUT_S so one stuck hotel never blocks
    the rest. RunLogger's first-finish-wins guard keeps the record consistent
    if an abandoned worker completes later."""
    from briefing.run_log import RunLogger
    run = RunLogger(hotel["id"], "data_only" if data_only else "full", attempt=attempt)
    run.start()

    result: dict = {}
    worker = threading.Thread(target=_process_hotel_locked,
                              args=(hotel, run, force, data_only, result, manual), daemon=True)
    worker.start()
    worker.join(_SOFT_WARN_S)
    if worker.is_alive():
        log.warning(f"[processor] {hotel['name']} — still running after {_SOFT_WARN_S}s (soft warn)")
        run.timings["warned_slow"] = True
        worker.join(max(1, _HARD_TIMEOUT_S - _SOFT_WARN_S))
        if worker.is_alive():
            log.error(f"[processor] {hotel['name']} — exceeded hard timeout "
                      f"({_HARD_TIMEOUT_S}s); abandoning run.")
            run.finish("failed", error_type="hard_timeout",
                       error_message=f"run exceeded {_HARD_TIMEOUT_S}s")
            return "failed"
    return result.get("status", "failed")


def _process_hotel_locked(hotel: dict, run, force: bool, data_only: bool, result: dict,
                          manual: bool = False) -> None:
    try:
        with run.stage("fetch"):
            data = _fetch_hotel_data(hotel, run)
        data["hotel_name"] = hotel["name"]
        yd = data.get("yesterday", {})
        log.info(f"[processor] Data fetched — yd_rev=€{yd.get('revenue',0):,.0f} occ={yd.get('occupancy',0)*100:.1f}% pace_months={len(data.get('pace',[]))} channels={len(data.get('topChannels',[]))}")

        from db.contract import is_publishable
        ok, reason = is_publishable(data, hotel.get("total_rooms"))
        dq = data.get("data_quality") or {}
        run.record(data_quality=dq or None, rows_fetched=dq.get("rows_fetched"))
        if not ok:
            log.warning(f"[processor] {hotel['name']} — snapshot not publishable ({reason}); "
                        f"keeping previous briefing.")
            run.finish("failed", error_type="data_quality", error_message=reason)
            result["status"] = "failed"  # retried — transient PMS states can clear
            return
        if dq.get("legacy_mode"):
            log.info(f"[processor] {hotel['name']} — legacy-mode snapshot (old fetcher, "
                     f"no signal fields).")

        import config
        config.HOTEL_NAME      = hotel["name"]
        config.TOTAL_ROOMS     = hotel["total_rooms"]
        config.RECIPIENT_EMAIL = hotel.get("recipient_email", "")
        config.RECIPIENT_NAME  = hotel.get("recipient_name", "General Manager")

        degraded = False
        if data_only:
            # Reuse AI insights from the morning's full briefing — no Claude API call
            ai = _get_existing_ai_insights(hotel["id"])
            if not ai:
                log.warning(f"[processor] {hotel['name']} — no morning AI insights found, skipping data-only refresh.")
                run.finish("failed", error_type="no_morning_ai",
                           error_message="no AI insights from morning run to reuse")
                result["status"] = "skipped"  # retrying cannot create morning AI
                return
            log.info(f"[processor] Reusing morning AI insights ({len(ai.get('insights', []))} insights)")
        else:
            from briefing.analyst import generate_insights
            with run.stage("ai"):
                ai = generate_insights(data, hotel_id=hotel["id"])
            meta = ai.pop("_meta", None)
            if meta:
                usage = meta.get("usage", {})
                run.record(
                    cards_audit=meta.get("cards_audit"),
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read_tokens=usage.get("cache_read_tokens"),
                    cache_write_tokens=usage.get("cache_write_tokens"),
                    estimated_cost_usd=meta.get("estimated_cost_usd"),
                    model=meta.get("model"),
                    prompt_version=meta.get("prompt_version"),
                )
                degraded = meta.get("fallback_cards", 0) > 0
            log.info(f"[processor] AI insights generated: {len(ai.get('insights', []))} insights")

        # NOTE: the PWA renders briefings from rendered_html — storage must stay
        # until the PWA is updated to render from data (learned 2026-07-23,
        # incident: app showed "no briefing" when html was omitted).
        from briefing.mailer import save_preview
        with run.stage("render"):
            preview_path = f"/tmp/{hotel['name'].lower().replace(' ', '_')}_briefing.html"
            save_preview(data, ai, preview_path)
            rendered_html = Path(preview_path).read_text(encoding="utf-8")

        from briefing.cloud_push import push_to_cloud
        with run.stage("publish"):
            push_to_cloud(data, ai, rendered_html=rendered_html,
                          hotel_id=hotel["id"], source_run_id=run.run_id,
                          notify=not manual)

        # Email only on the SCHEDULED morning full briefing — manual refreshes
        # update the app silently (learned 2026-07-24: refresh storms mailed
        # the GM once per run)
        if not data_only and not manual and hotel.get("recipient_email"):
            from briefing.mailer import send
            send(data, ai)
            log.info(f"[processor] Email sent to {hotel['recipient_email']}")

        result["status"] = "degraded" if degraded else "success"
        run.finish(result["status"])
        log.info(f"[processor] Done: {hotel['name']}")

    except Exception as exc:
        log.error(f"[processor] Failed for {hotel['name']}: {exc}", exc_info=True)
        run.finish("failed", error_type=type(exc).__name__, error_message=exc)
        result["status"] = "failed"


def _poll_refresh_commands() -> None:
    """Cloud-side refresh-command poller — replaces the hotel daemon's role in
    the PWA refresh flow, so hotel servers need to run NOTHING but cloudflared.
    Claims commands atomically (status pending → running with a conditional
    update), so it coexists safely with any hotel daemon still polling during
    the transition — the per-hotel lock dedupes double delivery."""
    import requests as _req
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        log.warning("[cmd-poll] SUPABASE env missing — cloud command poller disabled.")
        return
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}",
               "Content-Type": "application/json"}
    log.info("[cmd-poll] Cloud command poller started — every 30s.")
    while True:
        time.sleep(30)
        try:
            r = _req.get(
                f"{supabase_url}/rest/v1/refresh_commands",
                params={"status": "eq.pending", "select": "id,hotel_id,type",
                        "order": "requested_at.asc", "limit": "3"},
                headers=headers, timeout=10,
            )
            r.raise_for_status()
            for cmd in r.json():
                # Atomic claim: only wins while still pending
                c = _req.patch(
                    f"{supabase_url}/rest/v1/refresh_commands",
                    params={"id": f"eq.{cmd['id']}", "status": "eq.pending"},
                    json={"status": "running"},
                    headers={**headers, "Prefer": "return=representation"},
                    timeout=10,
                )
                if not c.ok or not c.json():
                    continue  # a hotel daemon claimed it first — fine
                log.info(f"[cmd-poll] Claimed command {cmd['id'][:8]}… "
                         f"for hotel {cmd['hotel_id'][:8]}…")
                threading.Thread(
                    target=run_all_hotels,
                    kwargs={"hotel_id_filter": cmd["hotel_id"], "cmd_id": cmd["id"],
                            "force": True, "manual": True},
                    daemon=True,
                ).start()
        except Exception as exc:
            log.warning(f"[cmd-poll] Poll error: {exc}")


def _mark_cmd_done(cmd_id: str) -> None:
    import requests as _req
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key or not cmd_id:
        return
    try:
        _req.patch(
            f"{supabase_url}/rest/v1/refresh_commands",
            params={"id": f"eq.{cmd_id}"},
            json={"status": "done"},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}",
                     "Content-Type": "application/json"},
            timeout=10,
        )
        log.info(f"[railway] Marked refresh command {cmd_id[:8]}… done")
    except Exception as e:
        log.warning(f"[railway] Failed to mark cmd done: {e}")


def run_all_hotels(hotel_id_filter: str | None = None, cmd_id: str | None = None,
                   force: bool = False, data_only: bool = False,
                   manual: bool = False) -> None:
    hotels = _get_hotels()
    if hotel_id_filter:
        hotels = [h for h in hotels if h["id"] == hotel_id_filter]
    if not hotels:
        log.warning("[scheduler] No hotels configured.")
        return
    for hotel in hotels:
        status = process_hotel(hotel, force=force, data_only=data_only, manual=manual)
        if status == "failed":
            _schedule_retry(hotel["id"], data_only, next_attempt=2, manual=manual)
    if cmd_id:
        _mark_cmd_done(cmd_id)


# ── Health check HTTP server ──────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/trigger"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            hotel_id  = (qs.get("hotel_id")   or [None])[0]
            cmd_id    = (qs.get("cmd_id")      or [None])[0]
            data_only = (qs.get("data_only")   or ["false"])[0].lower() == "true"
            threading.Thread(
                target=run_all_hotels,
                kwargs={"hotel_id_filter": hotel_id, "cmd_id": cmd_id, "force": True,
                        "data_only": data_only, "manual": True},
                daemon=True,
            ).start()
            body = b'{"status":"triggered"}'
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    import sys, traceback
    try:
        scheduler = BackgroundScheduler()
        # 03:30 UTC = 06:30 Greece — full briefing (replaces the hotel Task
        # Scheduler triggers so hotel servers can be decommissioned)
        scheduler.add_job(run_all_hotels, "cron", hour=3, minute=30)
        # 06:00 UTC = 09:00 Greece — catch-up full run (skips if briefing exists)
        scheduler.add_job(run_all_hotels, "cron", hour=6, minute=0)
        # 11:00 UTC = 14:00 Greece — data refresh, reuse morning AI insights
        scheduler.add_job(lambda: run_all_hotels(data_only=True), "cron", hour=11, minute=0)
        # 17:00 UTC = 20:00 Greece — data refresh, reuse morning AI insights
        scheduler.add_job(lambda: run_all_hotels(data_only=True), "cron", hour=17, minute=0)
        scheduler.start()
        log.info("[railway] Scheduler — 03:30 full | 06:00 catch-up | 11:00 + 17:00 UTC data-only")
        log.info(f"[railway] Hotels configured: {[h['name'] for h in _get_hotels()]}")

        # Cloud-side refresh-command poller (hotel daemons no longer needed)
        threading.Thread(target=_poll_refresh_commands, daemon=True).start()

        port = int(os.getenv("PORT", "8080"))
        log.info(f"[railway] HTTP server on port {port}")
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
