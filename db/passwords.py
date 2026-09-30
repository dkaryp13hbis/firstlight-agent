"""
Password hashing + session token helpers for the own login system (C3,
2026-09-30). Stdlib only — no new dependency can break the Docker build.

- Hash format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`` (OWASP-listed
  memory-hard KDF; the prefix leaves room for argon2id later without a
  migration — verify() dispatches on it).
- Session tokens: opaque, ``fl_`` prefix so the API can tell them from a
  Supabase JWT during the parallel-run; only the sha256 is stored.
- Initial passwords are generated from an unambiguous alphabet (no 0/O,
  1/l/I) because they are read out by phone — never emailed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32   # ~32 MB, ~50 ms on the Railway box
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
MIN_PASSWORD_LEN = 10


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time compare; False for any malformed/unknown hash."""
    if not stored or not password:
        return False
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt),
                            n=int(n), r=int(r), p=int(p), dklen=len(_unb64(digest)),
                            maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(dk, _unb64(digest))
    except (ValueError, TypeError):
        return False


# A real hash of a random secret, verified against every failed lookup so an
# unknown email costs the same time as a wrong password (no user enumeration).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def burn_time() -> None:
    verify_password("x", _DUMMY_HASH)


def password_problem(pw: str) -> str | None:
    """None when the password is acceptable, else the reason (plain words)."""
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        return "Password must contain both letters and numbers."
    return None


def generate_password(length: int = 12) -> str:
    """Phone-friendly initial password (always passes password_problem)."""
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if password_problem(pw) is None:
            return pw


def new_session_token() -> str:
    return "fl_" + secrets.token_urlsafe(32)


def is_session_token(bearer: str) -> bool:
    return bearer.startswith("fl_")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
