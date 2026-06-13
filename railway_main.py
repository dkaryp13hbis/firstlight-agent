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
    """
    Load hotel configs from env vars.
    Each hotel is a dict with keys: name, bridge_url, bridge_secret,
    total_rooms, recipient_email, recipient_name, supabase_hotel_id.
    Add more hotels here or migrate to Supabase hotels table later.
    """
    hotels = []
    # Hotel 1 — Pomegranate
    if os.getenv("POME_BRIDGE_URL"):
        hotels.append({
            "name":             os.getenv("HOTEL_NAME", "Pomegranate"),
            "total_rooms":      int(os.getenv("HOTEL_TOTAL_ROOMS", "167")),
            "bridge_url":       os.getenv("POME_BRIDGE_URL"),
            "bridge_secret":    os.getenv("POME_BRIDGE_SECRET", ""),
            "recipient_email":  os.getenv("RECIPIENT_EMAIL", ""),
            "recipient_name":   os.getenv("RECIPIENT_NAME", "General Manager"),
            "supabase_hotel_id": os.getenv("SUPABASE_HOTEL_ID", ""),
        })
    return hotels


def process_hotel(hotel: dict) -> None:
    log.info(f"[processor] Starting: {hotel['name']}")
    try:
        from briefing.bridge_fetcher import fetch_from_bridge
        data = fetch_from_bridge(hotel["bridge_url"], hotel["bridge_secret"])
        log.info(f"[processor] Data fetched for {hotel['name']}")

        # Override config for this hotel so analyst/mailer use correct values
        import config
        config.HOTEL_NAME  = hotel["name"]
        config.TOTAL_ROOMS = hotel["total_rooms"]
        config.RECIPIENT_EMAIL = hotel["recipient_email"]
        config.RECIPIENT_NAME  = hotel["recipient_name"]

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


def run_all_hotels() -> None:
    hotels = _get_hotels()
    if not hotels:
        log.warning("[scheduler] No hotels configured.")
        return
    for hotel in hotels:
        process_hotel(hotel)


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

        elif self.path == "/trigger":
            # Manual trigger — runs all hotels in background
            threading.Thread(target=run_all_hotels, daemon=True).start()
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
    # Daily scheduler — 06:00 UTC (adjust per hotel timezone later)
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_all_hotels, "cron", hour=6, minute=0)
    scheduler.start()
    log.info("[railway] Scheduler started — daily at 06:00 UTC")
    log.info(f"[railway] Hotels configured: {[h['name'] for h in _get_hotels()]}")

    port = int(os.getenv("PORT", "8080"))
    log.info(f"[railway] HTTP server on port {port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
