"""
Send a test push notification to all registered devices.
Run from: C:\FirstLight\firstlight-agent-main
    python test_push.py
"""
import json
import os
import sys
from dotenv import load_dotenv

load_dotenv()

import requests
from pywebpush import webpush, WebPushException

vapid_private     = os.getenv("VAPID_PRIVATE_KEY", "")
vapid_email       = os.getenv("VAPID_EMAIL", "mailto:dk@bi-automations.com")
supabase_url      = os.getenv("SUPABASE_URL", "").rstrip("/")
supabase_key      = os.getenv("SUPABASE_SERVICE_KEY", "")
supabase_hotel_id = os.getenv("SUPABASE_HOTEL_ID", "")

missing = [k for k, v in {
    "VAPID_PRIVATE_KEY":    vapid_private,
    "SUPABASE_URL":         supabase_url,
    "SUPABASE_SERVICE_KEY": supabase_key,
    "SUPABASE_HOTEL_ID":    supabase_hotel_id,
}.items() if not v]

if missing:
    print(f"ERROR: Missing in .env: {', '.join(missing)}")
    sys.exit(1)

# Fetch all push subscriptions
r = requests.get(
    f"{supabase_url}/rest/v1/push_subscriptions",
    params={"hotel_id": f"eq.{supabase_hotel_id}", "select": "subscription,hotel_id"},
    headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
    timeout=10,
)
r.raise_for_status()
subs = r.json()

if not subs:
    print("No push subscriptions found in Supabase.")
    print("Make sure you tapped the 🔔 bell in the PWA on your phone first.")
    sys.exit(0)

print(f"Found {len(subs)} subscription(s). Sending test notification...")

payload = json.dumps({
    "title": "FirstLight — Test Notification",
    "body":  "Push notifications are working! Tap to open AI Insights.",
    "sectionId": "sec-ai",
})

sent, failed = 0, 0
for row in subs:
    sub_info = row.get("subscription")
    if not sub_info:
        continue
    try:
        webpush(
            subscription_info=sub_info,
            data=payload,
            vapid_private_key=vapid_private,
            vapid_claims={"sub": vapid_email},
        )
        print(f"  Sent to hotel_id={row.get('hotel_id')}")
        sent += 1
    except WebPushException as e:
        print(f"  FAILED: {e}")
        if e.response is not None:
            print(f"  Response: {e.response.status_code} {e.response.text}")
        failed += 1

print(f"\nDone: {sent} sent, {failed} failed.")
