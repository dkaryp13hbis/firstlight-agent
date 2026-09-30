"""
User admin CLI for the own login system (C3). Runs INSIDE the web container
so it reaches the Railway Postgres and never needs a key on a laptop:

  railway ssh -s web -- python scripts/users.py list
  railway ssh -s web -- python scripts/users.py create --email a@b.gr --name Dinos
        --group tor-hotel-group --role owner [--password Xyz...] [--lang el] [--keep-password]
  railway ssh -s web -- python scripts/users.py grant --email a@b.gr --hotel <uuid> [--role viewer]
  railway ssh -s web -- python scripts/users.py reset --email a@b.gr [--password ...] [--keep-password]
  railway ssh -s web -- python scripts/users.py deactivate --email a@b.gr
  railway ssh -s web -- python scripts/users.py activate --email a@b.gr

Passwords are printed ONCE -- read them out by phone, never email them.
By default a created/reset password must be changed at first login;
--keep-password skips that (demo accounts).
"""
import argparse
import sys

sys.path.insert(0, ".")
from db import auth, store  # noqa: E402


def _user(email: str) -> dict:
    u = store.user_by_email(email)
    if not u:
        sys.exit(f"no user {email}")
    return u


def _scope(args) -> tuple[str, str] | None:
    if args.group:
        return ("group", store.scope_id_by_slug("group", args.group) or args.group)
    if args.org:
        return ("org", store.scope_id_by_slug("org", args.org) or args.org)
    if args.hotel:
        return ("hotel", args.hotel)
    return None


def _hotels(user_id: str) -> str:
    names = [h["name"] for h in (store.hotels_by_ids(store.hotel_access_ids(user_id) or []) or [])]
    return ", ".join(names) or "(none)"


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--email", required=True)
    c.add_argument("--name")
    c.add_argument("--password")
    c.add_argument("--lang", default="en")
    c.add_argument("--admin", action="store_true")
    c.add_argument("--keep-password", action="store_true")
    g = sub.add_parser("grant")
    g.add_argument("--email", required=True)
    for x in (c, g):
        x.add_argument("--group")
        x.add_argument("--org")
        x.add_argument("--hotel")
        x.add_argument("--role", default="viewer", choices=["owner", "viewer"])
    r = sub.add_parser("reset")
    r.add_argument("--email", required=True)
    r.add_argument("--password")
    r.add_argument("--keep-password", action="store_true")
    for name in ("deactivate", "activate"):
        sub.add_parser(name).add_argument("--email", required=True)
    sub.add_parser("list")
    a = p.parse_args()

    if not store.enabled():
        sys.exit("STORAGE/DATABASE_URL not set -- run inside the web container (railway ssh -s web)")

    try:
        if a.cmd == "create":
            out = auth.create_user(a.email, a.name, a.password, a.lang, a.admin,
                                   must_change=not a.keep_password)
            print(f"created {out['email']} id={out['id']}")
            print(f"initial password: {out['initial_password']}")
            sc = _scope(a)
            if sc:
                m = auth.grant(out["id"], sc[0], sc[1], a.role)
                print(f"access: {m['role']} of {m['scope_type']} '{m['scope_name']}'")
            print("hotels:", _hotels(out["id"]))
        elif a.cmd == "grant":
            u = _user(a.email)
            sc = _scope(a)
            if not sc:
                sys.exit("give --group <slug|uuid> | --org <slug|uuid> | --hotel <uuid>")
            m = auth.grant(u["id"], sc[0], sc[1], a.role)
            print(f"access: {m['role']} of {m['scope_type']} '{m['scope_name']}'")
            print("hotels:", _hotels(u["id"]))
        elif a.cmd == "reset":
            u = _user(a.email)
            new = auth.reset_password(u["id"], a.password, must_change=not a.keep_password)
            print(f"new password for {u['email']}: {new}")
        elif a.cmd in ("deactivate", "activate"):
            u = _user(a.email)
            store.user_set_active(u["id"], a.cmd == "activate")
            if a.cmd == "deactivate":
                store.sessions_revoke_user(u["id"])
            print(f"{a.cmd}d {u['email']}")
        elif a.cmd == "list":
            for u in store.users_list() or []:
                flags = [f for f, on in (("admin", u["is_platform_admin"]),
                                          ("inactive", not u["active"]),
                                          ("must-change", u["must_change_password"])) if on]
                print(f"{u['email']:40} {u.get('display_name') or '':20} {' '.join(flags):22} "
                      f"last login {u['last_login_at'] or '-'}  sessions {u['sessions_live']}")
                for m in u["memberships"]:
                    print(f"    {m['role']:6} {m['scope_type']:5} {m['scope_name']}")
    except auth.AuthError as e:
        sys.exit(f"error: {e.message}")


if __name__ == "__main__":
    main()
