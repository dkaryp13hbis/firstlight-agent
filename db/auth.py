"""
Own login system — the flows behind /auth/* (Phase C3, 2026-09-30).

Pure orchestration over db.store + db.passwords so it is testable without
FastAPI or a database (tests monkeypatch the store functions). Rules:
- One generic failure message for unknown email / wrong password / inactive
  user (no enumeration); a locked account says so (the user can wait it out).
- Lockout: 10 failed attempts -> 15 minutes. Successful login resets it.
- Sessions: opaque token, 30-day expiry sliding on use, revocable.
- Changing the password revokes every OTHER session of that user.
"""
from __future__ import annotations

from datetime import datetime, timezone

from db import passwords as pw
from db import store

SESSION_DAYS = 30
LOCK_AFTER, LOCK_MINUTES = 10, 15
GENERIC = "Wrong email or password."


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def public(u: dict) -> dict:
    """What the app may know about a user (never the hash / counters)."""
    return {"id": u["id"], "email": u["email"], "display_name": u.get("display_name"),
            "language": u.get("language") or "en",
            "is_platform_admin": bool(u.get("is_platform_admin")),
            "must_change_password": bool(u.get("must_change_password"))}


def _locked(u: dict) -> bool:
    lu = u.get("locked_until")
    if not lu:
        return False
    try:
        until = datetime.fromisoformat(str(lu))
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def login(email: str, password: str, user_agent: str | None = None) -> dict:
    if not store.enabled():
        raise AuthError(503, "Login is not available right now.")
    email = (email or "").strip().lower()
    if not email or not password:
        raise AuthError(401, GENERIC)
    u = store.user_by_email(email)
    if u is None:
        pw.burn_time()
        raise AuthError(401, GENERIC)
    if _locked(u):
        raise AuthError(423, f"Too many attempts. Try again in {LOCK_MINUTES} minutes.")
    if not u.get("active"):
        pw.burn_time()
        raise AuthError(401, GENERIC)
    if not pw.verify_password(password, u.get("password_hash")):
        n = store.user_login_failed(u["id"], LOCK_AFTER, LOCK_MINUTES)
        if n is not None and n >= LOCK_AFTER:
            raise AuthError(423, f"Too many attempts. Try again in {LOCK_MINUTES} minutes.")
        raise AuthError(401, GENERIC)
    token = pw.new_session_token()
    expires = store.session_create(u["id"], pw.token_hash(token), SESSION_DAYS,
                                   (user_agent or "")[:200] or None)
    if expires is None:
        raise AuthError(503, "Login is not available right now.")
    store.user_login_ok(u["id"])
    return {"token": token, "expires_at": expires, "user": public(u)}


def verify_session(token: str) -> dict | None:
    """User (public fields + id) for a live session token, else None."""
    if not pw.is_session_token(token) or not store.enabled():
        return None
    u = store.session_user(pw.token_hash(token), SESSION_DAYS)
    return public(u) if u else None


def logout(token: str) -> None:
    if pw.is_session_token(token):
        store.session_revoke(pw.token_hash(token))


def change_password(user_id: str, current: str, new: str, keep_token: str | None) -> dict:
    u = store.user_by_id(user_id)
    if not u:
        raise AuthError(404, "User not found.")
    if not pw.verify_password(current or "", u.get("password_hash")):
        raise AuthError(401, "Current password is wrong.")
    problem = pw.password_problem(new)
    if problem:
        raise AuthError(422, problem)
    if pw.verify_password(new, u.get("password_hash")):
        raise AuthError(422, "New password must differ from the current one.")
    if not store.user_set_password(user_id, pw.hash_password(new), False):
        raise AuthError(503, "Could not save the new password.")
    store.sessions_revoke_user(user_id, pw.token_hash(keep_token) if keep_token else None)
    u["must_change_password"] = False
    return public(u)


# -- admin helpers (portal + scripts/users.py) --------------------------------

def create_user(email: str, display_name: str | None, password: str | None = None,
                language: str = "en", is_admin: bool = False,
                must_change: bool = True) -> dict:
    """Creates the user; returns {'id', 'email', 'initial_password'} -- the
    password is shown ONCE (read it out by phone), never stored in clear."""
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError(422, "A valid email is required (it is the login name).")
    if password is None:
        password = pw.generate_password()
    else:
        problem = pw.password_problem(password)
        if problem:
            raise AuthError(422, problem)
    res = store.user_create(email, pw.hash_password(password), display_name,
                            language if language in ("en", "el") else "en",
                            must_change, is_admin)
    if res is None:
        raise AuthError(503, "User storage is not available.")
    if res.get("error") == "duplicate":
        raise AuthError(409, "A user with this email already exists.")
    return {"id": res["id"], "email": email, "initial_password": password}


def reset_password(user_id: str, password: str | None = None,
                   must_change: bool = True) -> str:
    """Admin reset: new (or generated) password, forced change by default,
    every session signed out."""
    if password is None:
        password = pw.generate_password()
    else:
        problem = pw.password_problem(password)
        if problem:
            raise AuthError(422, problem)
    if not store.user_set_password(user_id, pw.hash_password(password), must_change):
        raise AuthError(503, "User storage is not available.")
    store.sessions_revoke_user(user_id)
    return password


def grant(user_id: str, scope_type: str, scope_id: str, role: str = "viewer") -> dict:
    if scope_type not in ("group", "org", "hotel"):
        raise AuthError(422, "scope_type must be group|org|hotel")
    if role not in ("owner", "viewer"):
        raise AuthError(422, "role must be owner|viewer")
    name = store.scope_name(scope_type, scope_id)
    if not name:
        raise AuthError(404, f"{scope_type} {scope_id} not found")
    res = store.membership_add(user_id, scope_type, scope_id, role)
    if res is None:
        raise AuthError(503, "User storage is not available.")
    return {"id": res["id"], "scope_type": scope_type, "scope_id": scope_id,
            "scope_name": name, "role": role}
