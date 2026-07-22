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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("railway")

# Serialize hotel processing — prevents concurrent threads from clobbering
# shared global config/env state (config.HOTEL_NAME, SUPABASE_HOTEL_ID, etc.)
_process_lock = threading.Lock()


def _get_hotels() -> list[dict]:
    """Load active hotel configs from Supabase hotels table."""
    import requests as _req
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        log.error("[hotels] SUPABASE_URL or SUPABASE_SERVICE_KEY not set")
        return []
    try:
        resp = _req.get(
            f"{supabase_url}/rest/v1/hotels",
            params={"active": "eq.true", "select": "id,name,total_rooms,bridge_url,bridge_secret,recipient_email,recipient_name"},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        hotels = resp.json()
        log.info(f"[hotels] Loaded {len(hotels)} active hotels from Supabase")
        return hotels
    except Exception as exc:
        log.error(f"[hotels] Failed to load from Supabase: {exc}")
        return []


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


def process_hotel(hotel: dict, force: bool = False, data_only: bool = False) -> None:
    log.info(f"[processor] Starting: {hotel['name']} (data_only={data_only})")

    # Full briefing: skip if already done today (unless forced)
    if not data_only and not force and _briefing_exists_today(hotel["id"]):
        log.info(f"[processor] Skipped {hotel['name']} — briefing for today already exists.")
        return

    with _process_lock:
        try:
            from briefing.bridge_fetcher import fetch_from_bridge
            data = fetch_from_bridge(hotel["bridge_url"], hotel["bridge_secret"])
            data["hotel_name"] = hotel["name"]
            yd = data.get("yesterday", {})
            log.info(f"[processor] Data fetched — yd_rev=€{yd.get('revenue',0):,.0f} occ={yd.get('occupancy',0)*100:.1f}% pace_months={len(data.get('pace',[]))} channels={len(data.get('topChannels',[]))}")

            if yd.get("revenue", 0) == 0 and yd.get("roomNights", 0) == 0:
                log.warning(f"[processor] {hotel['name']} — data looks empty (rev=0, rn=0), skipping to avoid saving bad briefing.")
                return

            import config
            config.HOTEL_NAME      = hotel["name"]
            config.TOTAL_ROOMS     = hotel["total_rooms"]
            config.RECIPIENT_EMAIL = hotel.get("recipient_email", "")
            config.RECIPIENT_NAME  = hotel.get("recipient_name", "General Manager")

            if data_only:
                # Reuse AI insights from the morning's full briefing — no Claude API call
                ai = _get_existing_ai_insights(hotel["id"])
                if not ai:
                    log.warning(f"[processor] {hotel['name']} — no morning AI insights found, skipping data-only refresh.")
                    return
                log.info(f"[processor] Reusing morning AI insights ({len(ai.get('insights', []))} insights)")
            else:
                from briefing.analyst import generate_insights
                ai = generate_insights(data, hotel_id=hotel["id"])
                log.info(f"[processor] AI insights generated: {len(ai.get('insights', []))} insights")

            from briefing.mailer import save_preview, send
            preview_path = f"/tmp/{hotel['name'].lower().replace(' ', '_')}_briefing.html"
            save_preview(data, ai, preview_path)
            rendered_html = Path(preview_path).read_text(encoding="utf-8")

            from briefing.cloud_push import push_to_cloud
            push_to_cloud(data, ai, rendered_html=rendered_html, hotel_id=hotel["id"])

            # Only send email on the morning full briefing
            if not data_only and hotel.get("recipient_email"):
                send(data, ai)
                log.info(f"[processor] Email sent to {hotel['recipient_email']}")

            log.info(f"[processor] Done: {hotel['name']}")

        except Exception as exc:
            log.error(f"[processor] Failed for {hotel['name']}: {exc}", exc_info=True)


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


def run_all_hotels(hotel_id_filter: str | None = None, cmd_id: str | None = None, force: bool = False, data_only: bool = False) -> None:
    hotels = _get_hotels()
    if hotel_id_filter:
        hotels = [h for h in hotels if h["id"] == hotel_id_filter]
    if not hotels:
        log.warning("[scheduler] No hotels configured.")
        return
    for hotel in hotels:
        process_hotel(hotel, force=force, data_only=data_only)
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
                kwargs={"hotel_id_filter": hotel_id, "cmd_id": cmd_id, "force": True, "data_only": data_only},
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
        # 06:00 UTC = 09:00 Greece — full briefing with AI insights + email
        scheduler.add_job(run_all_hotels, "cron", hour=6, minute=0)
        # 11:00 UTC = 14:00 Greece — data refresh, reuse morning AI insights
        scheduler.add_job(lambda: run_all_hotels(data_only=True), "cron", hour=11, minute=0)
        # 17:00 UTC = 20:00 Greece — data refresh, reuse morning AI insights
        scheduler.add_job(lambda: run_all_hotels(data_only=True), "cron", hour=17, minute=0)
        scheduler.start()
        log.info("[railway] Scheduler started — 06:00 full briefing | 11:00 + 17:00 UTC data-only refresh")
        log.info(f"[railway] Hotels configured: {[h['name'] for h in _get_hotels()]}")

        port = int(os.getenv("PORT", "8080"))
        log.info(f"[railway] HTTP server on port {port}")
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
