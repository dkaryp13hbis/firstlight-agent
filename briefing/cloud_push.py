"""
Pushes the daily briefing to the FirstLight cloud API and sends push notifications.
Requires FIRSTLIGHT_API_URL and FIRSTLIGHT_API_KEY in .env.
For push notifications also requires VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY,
VAPID_EMAIL, SUPABASE_URL, and SUPABASE_SERVICE_KEY.
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Any

import requests

import config


def push_to_cloud(data: dict[str, Any], ai: dict[str, Any], rendered_html: str | None = None,
                  hotel_id: str | None = None, source_run_id: str | None = None) -> bool:
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if hotel_id is None:
        hotel_id = os.getenv("SUPABASE_HOTEL_ID", "")

    if not all([supabase_url, supabase_key, hotel_id]):
        print("[cloud] Skipped — SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_HOTEL_ID not set.")
        return False

    yesterday = date.today() - timedelta(days=1)

    # Never persist analyst audit metadata into the customer-facing briefing
    ai = {k: v for k, v in ai.items() if k != "_meta"}

    payload = {
        "hotel_id":     hotel_id,
        "report_date":  str(yesterday),
        "data":         data,
        "ai_insights":  ai,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    # JSON is canonical; HTML is only stored if a caller explicitly provides it
    if rendered_html is not None:
        payload["rendered_html"] = rendered_html
    if source_run_id is not None:
        payload["source_run_id"] = source_run_id

    try:
        resp = requests.post(
            f"{supabase_url}/rest/v1/briefings?on_conflict=hotel_id,report_date",
            json=payload,
            headers={
                "apikey":        supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates,return=minimal",
            },
            timeout=30,
        )
        resp.raise_for_status()
        print(f"[cloud] Pushed briefing for {yesterday} (hotel {hotel_id[:8]}…) -> HTTP {resp.status_code}")
        _send_push_notifications(ai, hotel_id, hotel_name=data.get("hotel_name") or config.HOTEL_NAME)
        return True
    except requests.RequestException as exc:
        print(f"[cloud] Push failed: {exc}")
        return False


def _send_push_notifications(ai: dict[str, Any], hotel_id: str, hotel_name: str | None = None) -> None:
    vapid_private     = os.getenv("VAPID_PRIVATE_KEY", "")
    vapid_email       = os.getenv("VAPID_EMAIL", "mailto:dk@bi-automations.com")
    supabase_url      = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key      = os.getenv("SUPABASE_SERVICE_KEY", "")
    supabase_hotel_id = hotel_id or os.getenv("SUPABASE_HOTEL_ID", "")

    if not all([vapid_private, supabase_url, supabase_key, supabase_hotel_id]):
        print("[push] Skipped — VAPID_PRIVATE_KEY / SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_HOTEL_ID not set.")
        return

    # If VAPID_PRIVATE_KEY is a file path that doesn't exist (e.g. Windows path on Linux),
    # treat the value as PEM content and write it to a temp file.
    if not os.path.exists(vapid_private):
        import tempfile
        pem_content = vapid_private.replace("\\n", "\n")
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
        tmp.write(pem_content)
        tmp.close()
        vapid_private = tmp.name

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("[push] pywebpush not installed — run: pip install pywebpush")
        return

    # Build notification payload from top insight
    insights = ai.get("insights", [])
    hotel_name = hotel_name or config.HOTEL_NAME or "Hotel"
    if insights:
        top = insights[0]
        title = f"{hotel_name} · {top.get('title', 'Morning Briefing')}"[:80]
        bullets = top.get("bullets", [])
        body  = bullets[0] if bullets else top.get("title", "")
    else:
        title = f"{hotel_name} · Morning Briefing"
        body  = (ai.get("executive_summary") or "Your morning briefing is ready.")[:120]

    pwa_url = os.getenv("PWA_URL", "https://app.hbis.io")
    push_payload = json.dumps({"title": title, "body": body, "sectionId": "sec-ai", "url": pwa_url})

    # Fetch all push subscriptions for this hotel from Supabase
    try:
        r = requests.get(
            f"{supabase_url}/rest/v1/push_subscriptions",
            params={"hotel_id": f"eq.{supabase_hotel_id}", "select": "subscription"},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        r.raise_for_status()
        subscriptions = r.json()
    except Exception as exc:
        print(f"[push] Failed to fetch subscriptions: {exc}")
        return

    if not subscriptions:
        print("[push] No push subscriptions registered.")
        return

    sent = 0
    for row in subscriptions:
        sub_info = row.get("subscription")
        if not sub_info:
            continue
        try:
            webpush(
                subscription_info=sub_info,
                data=push_payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": vapid_email},
            )
            sent += 1
        except WebPushException as exc:
            if exc.response is not None and exc.response.status_code == 410:
                print(f"[push] Subscription expired (410) — consider pruning.")
            else:
                print(f"[push] WebPush error: {exc}")
        except Exception as exc:
            print(f"[push] Error sending to subscription: {exc}")

    print(f"[push] Sent {sent}/{len(subscriptions)} notifications.")
