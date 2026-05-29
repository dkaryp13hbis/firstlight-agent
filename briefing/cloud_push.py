"""
Pushes the daily briefing to the FirstLight cloud API.
Called from main.py after data fetch + AI insights are ready.
Requires FIRSTLIGHT_API_URL and FIRSTLIGHT_API_KEY in .env
"""

import os
from datetime import date, timedelta
from typing import Any

import requests

import config


def push_to_cloud(data: dict[str, Any], ai: dict[str, Any], rendered_html: str | None = None) -> bool:
    api_url = os.getenv("FIRSTLIGHT_API_URL", "").rstrip("/")
    api_key = os.getenv("FIRSTLIGHT_API_KEY", "")

    if not api_url or not api_key:
        print("[cloud] Skipped — FIRSTLIGHT_API_URL or FIRSTLIGHT_API_KEY not set.")
        return False

    yesterday = date.today() - timedelta(days=1)

    payload = {
        "report_date_iso": str(yesterday),
        "data": data,
        "ai_insights": ai,
        "rendered_html": rendered_html,
    }

    try:
        resp = requests.post(
            f"{api_url}/briefing",
            json=payload,
            headers={"x-api-key": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        print(f"[cloud] Pushed briefing for {yesterday} -> HTTP {resp.status_code}")
        return True
    except requests.RequestException as exc:
        print(f"[cloud] Push failed: {exc}")
        return False
