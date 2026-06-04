"""
Run this ONCE to generate your VAPID key pair for Web Push notifications.
Copy the output values into your .env file and into firstlight-pwa/index.html.

Usage:
    pip install pywebpush
    python generate_vapid.py
"""
from py_vapid import Vapid

v = Vapid()
v.generate_keys()

priv = v.private_key
pub  = v.public_key

print("=" * 60)
print("Add these to hotel-morning-briefing/.env:")
print(f"  VAPID_PRIVATE_KEY={priv}")
print(f"  VAPID_PUBLIC_KEY={pub}")
print(f"  VAPID_EMAIL=mailto:dk@bi-automations.com")
print(f"  SUPABASE_URL=https://tqfupsvymisnskiwtjut.supabase.co")
print(f"  SUPABASE_SERVICE_KEY=<your Supabase service role key>")
print()
print("Add this to firstlight-pwa/index.html  (VAPID_PUBLIC_KEY line):")
print(f"  const VAPID_PUBLIC_KEY = '{pub}';")
print("=" * 60)
