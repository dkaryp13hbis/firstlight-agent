"""
Run this ONCE to generate your VAPID key pair for Web Push notifications.
Saves the private key as vapid_private.pem in the project directory.

Usage:
    cd C:\FirstLight\firstlight-agent-main
    python generate_vapid.py
"""
import base64
import os
from py_vapid import Vapid
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

v = Vapid()
v.generate_keys()

pem_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vapid_private.pem")
v.save_key(pem_path)

pub = base64.urlsafe_b64encode(
    v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
).rstrip(b"=").decode()

print("=" * 60)
print(f"Private key saved to: {pem_path}")
print()
print("Add to .env:")
print(f"  VAPID_PRIVATE_KEY={pem_path}")
print(f"  VAPID_EMAIL=mailto:dk@bi-automations.com")
print(f"  SUPABASE_URL=https://tqfupsvymisnskiwtjut.supabase.co")
print(f"  SUPABASE_SERVICE_KEY=<your Supabase service role key>")
print()
print("Replace in firstlight-pwa/index.html:")
print(f"  const VAPID_PUBLIC_KEY = '{pub}';")
print("=" * 60)
