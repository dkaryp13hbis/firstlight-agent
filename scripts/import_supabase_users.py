"""
Move the remaining Supabase Auth accounts onto the FirstLight login (C3 step 4).

Each auth.users row becomes a `users` row with the SAME uuid (so feedback,
watchlist, push_subscriptions and usage_events stay joinable) plus one
`memberships (hotel, role)` row per hotel_users link. Founder emails
(ADMIN_EMAILS) become platform admins. Every imported user gets a fresh
initial password, printed ONCE — read it out by phone. Idempotent: users
that already exist are skipped, memberships are upserted.

    railway ssh -s web -- python scripts/import_supabase_users.py --dry-run
    railway ssh -s web -- python scripts/import_supabase_users.py

Afterwards set AUTH=own on the web service to close the Supabase JWT path.
"""
import argparse
import os
import sys

import requests

sys.path.insert(0, ".")
from db import passwords as pw  # noqa: E402
from db import store  # noqa: E402

ADMIN_EMAILS = {e.strip().lower() for e in os.getenv(
    "ADMIN_EMAILS", "dk@bi-automations.com,d.karypidis@hbis.io").split(",") if e.strip()}


def sb_get(path: str, params: dict | None = None):
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    r = requests.get(f"{url}/{path}", params=params or {},
                     headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    return r.json()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if not store.enabled():
        sys.exit("run inside the web container (STORAGE=pg + DATABASE_URL)")

    auth_users = sb_get("auth/v1/admin/users", {"per_page": "200"}).get("users", [])
    links = sb_get("rest/v1/hotel_users", {"select": "user_id,hotel_id,role"})
    by_user: dict[str, list[dict]] = {}
    for l in links:
        by_user.setdefault(l["user_id"], []).append(l)

    for u in auth_users:
        uid, email = u["id"], (u.get("email") or "").lower()
        if not email:
            continue
        is_admin = email in ADMIN_EMAILS
        existing = store.user_by_id(uid) or store.user_by_email(email)
        if existing:
            print(f"skip   {email} (already on FirstLight login)")
            continue
        hotels = by_user.get(uid, [])
        print(f"import {email}{' [platform admin]' if is_admin else ''} → {len(hotels)} hotel link(s)")
        if a.dry_run:
            continue
        password = pw.generate_password()
        store._exec(  # same uuid as Supabase Auth — keeps every user-keyed row joinable
            "insert into users (id, email, password_hash, display_name, is_platform_admin, "
            "must_change_password) values (%s,%s,%s,%s,%s,true) on conflict do nothing",
            (uid, email, pw.hash_password(password),
             (u.get("user_metadata") or {}).get("name"), is_admin))
        for l in hotels:
            role = "owner" if is_admin else ("owner" if l.get("role") == "owner" else "viewer")
            store.membership_add(uid, "hotel", l["hotel_id"], role)
        print(f"       initial password: {password}   (forced change at first login)")

    if a.dry_run:
        print("dry run — nothing written")
    else:
        print("done. Next: set AUTH=own on the web service once everyone has signed in.")


if __name__ == "__main__":
    main()
