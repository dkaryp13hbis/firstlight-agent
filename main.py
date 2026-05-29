"""
Hotel Morning Briefing — entry point.

Usage:
    python main.py              # fetch data, call Claude, send email
    python main.py --preview    # fetch data, render HTML preview (no email, AI optional)
    python main.py --dry-run    # fetch data only, print payload — skip Claude + email

AI is skipped automatically when ANTHROPIC_API_KEY is not set in .env.
"""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

import config
from db.connection import get_connection
from briefing.fetcher import fetch_briefing_data
from briefing.analyst import generate_insights, load_cached_insights
from briefing.mailer import send, save_preview
from briefing.cloud_push import push_to_cloud


def main(preview: bool = False, dry_run: bool = False, no_api: bool = False) -> None:
    print(f"[main] Connecting to {config.SQL_SERVER} — hotel {config.HOTEL_ID}")

    try:
        conn = get_connection()
    except Exception as exc:
        print(f"[main] DB connection failed: {exc}")
        sys.exit(1)

    print("[main] Fetching briefing data …")
    try:
        data = fetch_briefing_data(conn)
    except Exception as exc:
        print(f"[main] Data fetch failed: {exc}")
        sys.exit(1)
    finally:
        conn.close()

    if dry_run:
        print(json.dumps(data, indent=2, default=str))
        print("[main] Dry-run complete — no email sent.")
        return

    print("[main] Generating AI insights …")
    ai = load_cached_insights() if no_api else generate_insights(data)

    # Always render HTML so cloud always has the latest
    out = Path("preview.html")
    save_preview(data, ai, str(out))
    rendered_html = out.read_text(encoding="utf-8")

    print("[main] Pushing to FirstLight cloud …")
    push_to_cloud(data, ai, rendered_html=rendered_html)

    if preview:
        webbrowser.open(out.resolve().as_uri())
        print("[main] Preview mode — no email sent.")
        return

    print(f"[main] Sending email → {config.RECIPIENT_EMAIL} …")
    ok = send(data, ai)
    if not ok:
        sys.exit(1)

    print("[main] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hotel Morning Briefing")
    parser.add_argument("--preview",  action="store_true", help="Save HTML preview, skip email")
    parser.add_argument("--dry-run",  action="store_true", help="Print data payload, skip Claude + email")
    parser.add_argument("--no-api",   action="store_true", help="Use cached AI response, skip Claude API call")
    args = parser.parse_args()
    main(preview=args.preview, dry_run=args.dry_run, no_api=args.no_api)
