"""
Live preview server for Hotel Morning Briefing.

Run:  %%USERPROFILE%%\\anaconda3\\python.exe server.py
Open: http://localhost:8765

Click "Refresh Data" in the bottom-right corner to regenerate the
report from live Protel PMS data. The page auto-reloads when done.
"""

import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import requests as _requests
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent / ".env")
    _CLOUD_POLL = True
except ImportError:
    _CLOUD_POLL = False

PORT = 8765
PYTHON = sys.executable
PROJECT_DIR = Path(__file__).parent

_lock = threading.Lock()
_refreshing = False

_REFRESH_BTN = """
<div id="_srv_bar" style="
  position:fixed; bottom:24px; right:24px; z-index:9999;
  display:flex; align-items:center; gap:10px;
">
  <a href="/refresh" id="_srv_btn" style="
    display:inline-flex; align-items:center; gap:8px;
    padding:11px 22px; border-radius:999px;
    background:linear-gradient(135deg,#2E7CF7,#38E1F0);
    color:#fff; font-family:'IBM Plex Mono',monospace; font-size:11px; font-weight:600;
    text-decoration:none; letter-spacing:.06em;
    box-shadow:0 4px 18px -3px rgba(46,124,247,.6);
    transition:opacity .2s;
  "
  onmouseover="this.style.opacity='.85'"
  onmouseout="this.style.opacity='1'"
  >
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.5"
         stroke-linecap="round" stroke-linejoin="round">
      <path d="M1 4v6h6M23 20v-6h-6"/>
      <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4-4.64 4.36A9 9 0 0 1 3.51 15"/>
    </svg>
    Refresh Data
  </a>
</div>
"""

_LOADING_HTML = """\
<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<title>Refreshing&hellip;</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #061535; font-family: 'Outfit', sans-serif; color: #fff;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; flex-direction: column; gap: 22px;
  }
  .spin {
    width: 46px; height: 46px;
    border: 3px solid rgba(56,225,240,.18);
    border-top-color: #38E1F0;
    border-radius: 50%;
    animation: spin .75s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  h2 { font-size: 18px; font-weight: 600; letter-spacing: -.01em; }
  small {
    font-family: 'IBM Plex Mono', monospace; font-size: 10px;
    color: rgba(255,255,255,.35); letter-spacing: .1em; text-transform: uppercase;
  }
</style>
<script>
(function poll() {
  fetch('/status')
    .then(r => r.json())
    .then(d => {
      if (!d.refreshing) window.location.replace('/');
      else setTimeout(poll, 1500);
    })
    .catch(() => setTimeout(poll, 2000));
}());
</script>
</head>
<body>
  <div class="spin"></div>
  <h2>Fetching live data&hellip;</h2>
  <small>Protel PMS &middot; AI Insights &middot; ~15 sec</small>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence request logs

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        global _refreshing

        # ── /fetch ── called by Railway via Cloudflare Tunnel to get live SQL data
        if self.path == "/fetch":
            secret = os.getenv("BRIDGE_SECRET", "")
            if secret and self.headers.get("x-bridge-secret") != secret:
                self._send(401, "application/json", b'{"error":"unauthorized"}')
                return
            try:
                from db.connection import get_connection
                from briefing.fetcher import fetch_briefing_data
                conn = get_connection()
                data = fetch_briefing_data(conn)
                conn.close()
                body = json.dumps(data, default=str).encode()
                self._send(200, "application/json", body)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode()
                self._send(500, "application/json", body)

        # ── /status ── polled by loading page
        elif self.path == "/status":
            with _lock:
                r = _refreshing
            self._send(200, "application/json",
                       f'{{"refreshing":{str(r).lower()}}}'.encode())

        # ── /refresh ── trigger regeneration
        elif self.path == "/refresh":
            with _lock:
                already = _refreshing
                if not already:
                    _refreshing = True

            if not already:
                def _run() -> None:
                    global _refreshing
                    subprocess.run(
                        [PYTHON, str(PROJECT_DIR / "main.py"), "--no-api"],
                        cwd=str(PROJECT_DIR),
                        capture_output=True,
                    )
                    with _lock:
                        _refreshing = False

                threading.Thread(target=_run, daemon=True).start()

            self.send_response(302)
            self.send_header("Location", "/loading")
            self.end_headers()

        # ── /loading ── spinner while regenerating
        elif self.path == "/loading":
            self._send(200, "text/html; charset=utf-8",
                       _LOADING_HTML.encode("utf-8"))

        # ── / ── serve preview with injected refresh button
        elif self.path in ("/", "/index.html"):
            html = (PROJECT_DIR / "preview.html").read_bytes()
            html = html.replace(b"</body>",
                                _REFRESH_BTN.encode("utf-8") + b"</body>")
            self._send(200, "text/html; charset=utf-8", html)

        else:
            self.send_response(404)
            self.end_headers()


def _poll_commands() -> None:
    """Background thread: polls Supabase for refresh commands and delegates to Railway."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    hotel_id     = os.getenv("SUPABASE_HOTEL_ID", "")
    railway_url  = os.getenv("RAILWAY_URL", "").rstrip("/")

    if not all([supabase_url, supabase_key, hotel_id, railway_url]):
        missing = [k for k, v in {
            "SUPABASE_URL": supabase_url, "SUPABASE_SERVICE_KEY": supabase_key,
            "SUPABASE_HOTEL_ID": hotel_id, "RAILWAY_URL": railway_url,
        }.items() if not v]
        print(f"[cloud-poll] Skipped — missing: {', '.join(missing)}")
        return

    sb_headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type":  "application/json",
    }
    print(f"[cloud-poll] Started — polling every 30 s for hotel {hotel_id[:8]}…")

    while True:
        time.sleep(30)
        try:
            r = _requests.get(
                f"{supabase_url}/rest/v1/refresh_commands",
                params={"hotel_id": f"eq.{hotel_id}", "status": "eq.pending",
                        "select": "id,type", "limit": "1", "order": "created_at.asc"},
                headers=sb_headers, timeout=10,
            )
            r.raise_for_status()
            cmds = r.json()
            if not cmds:
                continue

            cmd_id   = cmds[0]["id"]
            cmd_type = cmds[0]["type"]
            print(f"[cloud-poll] Got command: {cmd_type} ({cmd_id[:8]})")

            # Mark running in Supabase so PWA shows spinner
            _requests.patch(
                f"{supabase_url}/rest/v1/refresh_commands",
                params={"id": f"eq.{cmd_id}"},
                json={"status": "running"},
                headers=sb_headers, timeout=10,
            )

            # Delegate to Railway — it fetches fresh data, generates insights, updates Supabase
            _requests.get(
                f"{railway_url}/trigger",
                params={"hotel_id": hotel_id, "cmd_id": cmd_id},
                timeout=15,
            )
            print(f"[cloud-poll] Delegated {cmd_id[:8]} to Railway.")

        except Exception as exc:
            print(f"[cloud-poll] Poll error: {exc}")


class _ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true",
                        help="Cloud-poll only — no HTTP server, no browser (for hotel server)")
    args = parser.parse_args()

    if args.daemon:
        # Headless mode: HTTP bridge on PORT + cloud-poll for refresh commands
        srv = _ThreadingServer(("localhost", PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[firstlight] Daemon started — bridge on localhost:{PORT}")
        if _CLOUD_POLL:
            _poll_commands()   # blocks forever
        else:
            print("[firstlight] FIRSTLIGHT_API_URL/KEY not set — bridge only.")
            threading.Event().wait()  # block forever so the bridge keeps running
    else:
        srv = _ThreadingServer(("localhost", PORT), Handler)
        url = f"http://localhost:{PORT}"
        print(f"[server] Hotel Morning Briefing -> {url}")
        print("[server] Press Ctrl+C to stop")
        if _CLOUD_POLL:
            threading.Thread(target=_poll_commands, daemon=True).start()
        webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[server] Stopped.")
