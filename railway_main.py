"""
Railway cloud processor entry point.
- Serves HTTP health check on $PORT (required by Railway)
- Runs daily hotel processing via APScheduler
- Fetches data from hotel bridges (via Cloudflare Tunnel) instead of direct SQL
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


def process_hotel(hotel: dict) -> None:
    log.info(f"[processor] Starting: {hotel['name']}")
    try:
        from briefing.bridge_fetcher import fetch_from_bridge
        data = fetch_from_bridge(hotel["bridge_url"], hotel["bridge_secret"])
        data["hotel_name"] = hotel["name"]  # authoritative name from Supabase
        yd = data.get("yesterday", {})
        log.info(f"[processor] Data fetched — yd_rev=€{yd.get('revenue',0):,.0f} occ={yd.get('occupancy',0)*100:.1f}% pace_months={len(data.get('pace',[]))} channels={len(data.get('topChannels',[]))}")

        # Override config for this hotel so analyst/mailer use correct values
        import config
        config.HOTEL_NAME  = hotel["name"]
        config.TOTAL_ROOMS = hotel["total_rooms"]
        config.RECIPIENT_EMAIL = hotel.get("recipient_email", "")
        config.RECIPIENT_NAME  = hotel.get("recipient_name", "General Manager")
        os.environ["SUPABASE_HOTEL_ID"] = hotel["id"]  # used by cloud_push

        from briefing.analyst import generate_insights
        ai = generate_insights(data)
        log.info(f"[processor] AI insights generated: {len(ai.get('insights', []))} insights")

        from briefing.mailer import save_preview, send
        preview_path = f"/tmp/{hotel['name'].lower()}_briefing.html"
        save_preview(data, ai, preview_path)
        rendered_html = Path(preview_path).read_text(encoding="utf-8")

        from briefing.cloud_push import push_to_cloud
        push_to_cloud(data, ai, rendered_html=rendered_html)

        if hotel["recipient_email"]:
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


def run_all_hotels(hotel_id_filter: str | None = None, cmd_id: str | None = None) -> None:
    hotels = _get_hotels()
    if hotel_id_filter:
        hotels = [h for h in hotels if h["id"] == hotel_id_filter]
    if not hotels:
        log.warning("[scheduler] No hotels configured.")
        return
    for hotel in hotels:
        process_hotel(hotel)
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
            hotel_id = (qs.get("hotel_id") or [None])[0]
            cmd_id   = (qs.get("cmd_id")   or [None])[0]
            threading.Thread(
                target=run_all_hotels,
                kwargs={"hotel_id_filter": hotel_id, "cmd_id": cmd_id},
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
        # Daily scheduler — 06:00 UTC (adjust per hotel timezone later)
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_all_hotels, "cron", hour=6, minute=0)
        scheduler.start()
        log.info("[railway] Scheduler started — daily at 06:00 UTC")
        log.info(f"[railway] Hotels configured: {[h['name'] for h in _get_hotels()]}")

        port = int(os.getenv("PORT", "8080"))
        log.info(f"[railway] HTTP server on port {port}")
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
