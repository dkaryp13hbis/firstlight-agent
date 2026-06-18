"""
Triggers the Railway cloud processor to generate this hotel's morning briefing.
Run this from Task Scheduler at the scheduled briefing time (e.g. 06:30 or 07:00).

Reads RAILWAY_URL and SUPABASE_HOTEL_ID from the local .env file, then calls
GET <RAILWAY_URL>/trigger?hotel_id=<id>

Railway does all the work: fetches from the bridge, generates AI insights,
pushes to Supabase, sends push notifications.
"""

import os
import sys

from dotenv import load_dotenv
import requests

load_dotenv()

railway_url = os.getenv("RAILWAY_URL", "").rstrip("/")
hotel_id    = os.getenv("SUPABASE_HOTEL_ID", "")
hotel_name  = os.getenv("HOTEL_NAME", "Hotel")

if not railway_url:
    print("[trigger] ERROR: RAILWAY_URL not set in .env — cannot trigger Railway.")
    sys.exit(1)
if not hotel_id:
    print("[trigger] ERROR: SUPABASE_HOTEL_ID not set in .env — cannot identify hotel.")
    sys.exit(1)

print(f"[trigger] Triggering briefing for {hotel_name} ({hotel_id[:8]}…)")
print(f"[trigger] Calling {railway_url}/trigger?hotel_id={hotel_id}")

try:
    resp = requests.get(
        f"{railway_url}/trigger",
        params={"hotel_id": hotel_id},
        timeout=300,  # Railway processing takes 1-3 minutes
    )
    print(f"[trigger] Response HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.ok:
        print("[trigger] Done — briefing generated and pushed to Supabase.")
    else:
        print(f"[trigger] WARNING: Railway returned non-2xx status.")
        sys.exit(1)
except requests.Timeout:
    print("[trigger] Request timed out after 5 minutes — Railway may still be processing.")
    sys.exit(1)
except Exception as exc:
    print(f"[trigger] ERROR: {exc}")
    sys.exit(1)
