# Own login system (C3) — hashing, tokens, login/lockout/session/change flows
# against an in-memory fake store (no database). Run: py -3.13 test_auth.py
import io, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
os.environ["STORAGE"] = "pg"
os.environ["DATABASE_URL"] = "postgresql://fake/fake"   # never connected: store is faked below

from datetime import datetime, timedelta, timezone
from db import passwords as pw
from db import store, auth

P = F = 0
def check(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  ok  {name}")
    else: F += 1; print(f"FAIL  {name} {detail}")

# ── passwords.py ─────────────────────────────────────────────────────────────
h = pw.hash_password("Sample-Pass-2026")
check("hash format scrypt$n$r$p$salt$hash", h.startswith("scrypt$") and h.count("$") == 5)
check("verify ok", pw.verify_password("Sample-Pass-2026", h))
check("verify wrong", not pw.verify_password("Other-Pass-2026", h))
check("verify empty / None safe", not pw.verify_password("", h) and not pw.verify_password("x", None))
check("verify malformed safe", not pw.verify_password("x", "argon2$zzz") and not pw.verify_password("x", "garbage"))
check("two hashes differ (salt)", pw.hash_password("abc") != pw.hash_password("abc"))
check("policy: short rejected", pw.password_problem("Ab1") is not None)
check("policy: letters only rejected", pw.password_problem("abcdefghijkl") is not None)
check("policy: digits only rejected", pw.password_problem("123456789012") is not None)
check("policy: ok", pw.password_problem("Sample-Pass-2026") is None)
g = pw.generate_password()
check("generated passes policy, phone-safe", pw.password_problem(g) is None and len(g) == 12
      and not any(c in "0O1lI" for c in g))
t = pw.new_session_token()
check("token prefix fl_ + long", t.startswith("fl_") and len(t) > 40 and pw.is_session_token(t))
check("JWT is not a session token", not pw.is_session_token("eyJhbGciOi..."))
check("token_hash sha256 hex", len(pw.token_hash(t)) == 64 and pw.token_hash(t) == pw.token_hash(t))

# ── fake store ───────────────────────────────────────────────────────────────
USERS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
MEMBERS: list[dict] = []
now = lambda: datetime.now(timezone.utc)

def _u(email):
    return next((u for u in USERS.values() if u["email"] == email.lower()), None)

store.user_by_email = lambda e: dict(_u(e)) if _u(e) else None
store.user_by_id = lambda i: dict(USERS[i]) if i in USERS else None
def _create(email, ph, name, lang="en", must_change=True, is_admin=False):
    if _u(email): return {"error": "duplicate"}
    uid = f"u{len(USERS)+1}"
    USERS[uid] = {"id": uid, "email": email.lower(), "password_hash": ph, "display_name": name,
                  "language": lang, "is_platform_admin": is_admin, "must_change_password": must_change,
                  "active": True, "failed_logins": 0, "locked_until": None, "created_at": None, "last_login_at": None}
    return {"id": uid}
store.user_create = _create
def _setpw(uid, ph, must):
    USERS[uid].update(password_hash=ph, must_change_password=must, failed_logins=0, locked_until=None); return True
store.user_set_password = _setpw
store.user_set_active = lambda uid, a: USERS[uid].update(active=a) or True
store.user_login_ok = lambda uid: USERS[uid].update(failed_logins=0, locked_until=None, last_login_at=now().isoformat())
def _failed(uid, lock_after, lock_minutes):
    u = USERS[uid]; u["failed_logins"] += 1
    if u["failed_logins"] >= lock_after:
        u["locked_until"] = (now() + timedelta(minutes=lock_minutes)).isoformat()
    return u["failed_logins"]
store.user_login_failed = _failed
def _screate(uid, th, days, ua):
    exp = (now() + timedelta(days=days)).isoformat()
    SESSIONS[th] = {"user_id": uid, "expires_at": exp, "revoked_at": None, "user_agent": ua}
    return exp
store.session_create = _screate
def _suser(th, slide):
    s = SESSIONS.get(th)
    if not s or s["revoked_at"] or datetime.fromisoformat(s["expires_at"]) < now(): return None
    u = USERS[s["user_id"]]
    if not u["active"]: return None
    return {**u, "session_id": th[:8], "expires_at": s["expires_at"]}
store.session_user = _suser
store.session_revoke = lambda th: SESSIONS.get(th, {}).update(revoked_at=now().isoformat())
def _revoke_all(uid, keep=None):
    for th, s in SESSIONS.items():
        if s["user_id"] == uid and th != keep: s["revoked_at"] = now().isoformat()
store.sessions_revoke_user = _revoke_all
SCOPES = {("group", "g-tor"): "Tor Hotel Group", ("hotel", "h1"): "City Hotel"}
store.scope_name = lambda st, sid: SCOPES.get((st, sid))
def _madd(uid, st, sid, role):
    MEMBERS.append({"id": f"m{len(MEMBERS)+1}", "user_id": uid, "scope_type": st, "scope_id": sid, "role": role})
    return {"id": MEMBERS[-1]["id"]}
store.membership_add = _madd
store.enabled = lambda: True

# ── create_user / grant ──────────────────────────────────────────────────────
try:
    auth.create_user("not-an-email", "x"); check("create: bad email rejected", False)
except auth.AuthError as e: check("create: bad email rejected", e.status == 422)
try:
    auth.create_user("a@b.gr", "x", password="short1"); check("create: weak password rejected", False)
except auth.AuthError as e: check("create: weak password rejected", e.status == 422)
out = auth.create_user("Dinos@TorHotelGroup.gr", "Dinos", password="Sample-Pass-2026", must_change=False)
check("create: lowercases email, returns id + password once", out["email"] == "dinos@torhotelgroup.gr"
      and out["id"] == "u1" and out["initial_password"] == "Sample-Pass-2026")
check("create: hash stored, not clear text", USERS["u1"]["password_hash"].startswith("scrypt$")
      and "Sample-Pass-2026" not in USERS["u1"]["password_hash"])
check("create: must_change honoured", USERS["u1"]["must_change_password"] is False)
gen = auth.create_user("gm@torhotelgroup.gr", "GM")
check("create: generated password when none given", pw.password_problem(gen["initial_password"]) is None
      and USERS["u2"]["must_change_password"] is True)
try:
    auth.create_user("dinos@torhotelgroup.gr", "dup"); check("create: duplicate → 409", False)
except auth.AuthError as e: check("create: duplicate → 409", e.status == 409)
m = auth.grant("u1", "group", "g-tor", "owner")
check("grant: resolves scope name", m["scope_name"] == "Tor Hotel Group" and m["role"] == "owner")
try:
    auth.grant("u1", "group", "nope"); check("grant: unknown scope → 404", False)
except auth.AuthError as e: check("grant: unknown scope → 404", e.status == 404)
try:
    auth.grant("u1", "team", "g-tor"); check("grant: bad scope type → 422", False)
except auth.AuthError as e: check("grant: bad scope type → 422", e.status == 422)

# ── login ────────────────────────────────────────────────────────────────────
r = auth.login("DINOS@torhotelgroup.gr ", "Sample-Pass-2026", "Mozilla/5.0 iPhone")
check("login: ok → token + user (no hash)", r["token"].startswith("fl_") and r["user"]["email"] == "dinos@torhotelgroup.gr"
      and "password_hash" not in r["user"] and r["user"]["must_change_password"] is False)
tok1 = r["token"]
check("login: session stored by hash only", pw.token_hash(tok1) in SESSIONS and tok1 not in SESSIONS)
check("login: last_login_at set, counter reset", USERS["u1"]["last_login_at"] and USERS["u1"]["failed_logins"] == 0)
for bad in ("", "x"):
    try:
        auth.login("dinos@torhotelgroup.gr", bad); check(f"login: empty/wrong password {bad!r} → 401", False)
    except auth.AuthError as e: check(f"login: empty/wrong password {bad!r} → 401 generic", e.status == 401 and e.message == auth.GENERIC)
try:
    auth.login("nobody@x.gr", "whatever12"); check("login: unknown email → same generic 401", False)
except auth.AuthError as e: check("login: unknown email → same generic 401", e.status == 401 and e.message == auth.GENERIC)

# lockout after 10 failures, then unlock by time
USERS["u2"]["failed_logins"] = 0
for i in range(9):
    try: auth.login("gm@torhotelgroup.gr", "wrong-pass-1")
    except auth.AuthError as e: last = e
check("lockout: 9 failures still 401", last.status == 401 and USERS["u2"]["locked_until"] is None)
try: auth.login("gm@torhotelgroup.gr", "wrong-pass-1")
except auth.AuthError as e: last = e
check("lockout: 10th failure → 423 locked", last.status == 423 and USERS["u2"]["locked_until"])
try: auth.login("gm@torhotelgroup.gr", gen["initial_password"])
except auth.AuthError as e: last = e
check("lockout: correct password while locked → 423", last.status == 423)
USERS["u2"]["locked_until"] = (now() - timedelta(minutes=1)).isoformat()
r2 = auth.login("gm@torhotelgroup.gr", gen["initial_password"])
check("lockout: expired lock → login ok, counter reset", r2["token"] and USERS["u2"]["failed_logins"] == 0
      and r2["user"]["must_change_password"] is True)

# inactive user
USERS["u2"]["active"] = False
try: auth.login("gm@torhotelgroup.gr", gen["initial_password"]); check("inactive → 401 generic", False)
except auth.AuthError as e: check("inactive → 401 generic", e.status == 401 and e.message == auth.GENERIC)
check("inactive: existing session dead", auth.verify_session(r2["token"]) is None)
USERS["u2"]["active"] = True

# ── sessions ─────────────────────────────────────────────────────────────────
u = auth.verify_session(tok1)
check("verify_session: live token → public user", u and u["id"] == "u1" and "password_hash" not in u)
check("verify_session: JWT-shaped token → None (no DB call)", auth.verify_session("eyJhbGciOi.xx.yy") is None)
check("verify_session: unknown fl_ token → None", auth.verify_session("fl_unknown") is None)
auth.logout(tok1)
check("logout: token revoked", auth.verify_session(tok1) is None)
auth.logout("eyJnot-a-session")   # must not raise

# ── change password ──────────────────────────────────────────────────────────
a = auth.login("dinos@torhotelgroup.gr", "Sample-Pass-2026")["token"]
b = auth.login("dinos@torhotelgroup.gr", "Sample-Pass-2026")["token"]
for cur, new, code, label in (("wrong", "NewPassword2026", 401, "wrong current"),
                              ("Sample-Pass-2026", "short1", 422, "weak new"),
                              ("Sample-Pass-2026", "Sample-Pass-2026", 422, "same as current")):
    try: auth.change_password("u1", cur, new, a); check(f"change: {label} rejected", False)
    except auth.AuthError as e: check(f"change: {label} rejected ({code})", e.status == code)
res = auth.change_password("u1", "Sample-Pass-2026", "NewPassword2026", a)
check("change: ok → must_change false", res["must_change_password"] is False and USERS["u1"]["must_change_password"] is False)
check("change: old password dead, new works", pw.verify_password("NewPassword2026", USERS["u1"]["password_hash"])
      and not pw.verify_password("Sample-Pass-2026", USERS["u1"]["password_hash"]))
check("change: current session kept, other session revoked",
      auth.verify_session(a) is not None and auth.verify_session(b) is None)

# ── admin reset ──────────────────────────────────────────────────────────────
newpw = auth.reset_password("u1")
check("reset: generated password, forced change, all sessions out",
      pw.password_problem(newpw) is None and USERS["u1"]["must_change_password"] is True
      and auth.verify_session(a) is None)
newpw2 = auth.reset_password("u1", "Sample-Pass-2026", must_change=False)
check("reset: explicit password + keep", newpw2 == "Sample-Pass-2026" and USERS["u1"]["must_change_password"] is False)

# ── store off → 503, never open ──────────────────────────────────────────────
store.enabled = lambda: False
try: auth.login("dinos@torhotelgroup.gr", "Sample-Pass-2026"); check("store off: login 503", False)
except auth.AuthError as e: check("store off: login 503", e.status == 503)
check("store off: verify_session None", auth.verify_session(a) is None)

print(f"\n{P} passed, {F} failed")
raise SystemExit(1 if F else 0)
