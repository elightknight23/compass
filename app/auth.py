"""Authentication — real accounts, signed sessions, one place to ask "who?".

Identity comes from a verified credential and nowhere else. A message that
says "I'm the warden" changes nothing; neither does a forged header. The
handlers read `state["user"]["role"]`, and this module is the only thing
that decides what that user is.

**What's real here.** Passwords are never stored — only a scrypt hash with
a per-user random salt, compared in constant time. Sessions are HMAC-SHA256
signed tokens carrying a user id and an expiry; a tampered or expired token
verifies false and the request is anonymous. The cookie is HttpOnly, so
page JavaScript cannot read it.

**Authorization is two questions, not one.** `is_staff` answers "is this a
staff account at all?" and gates *reading* other people's records.
`may_action` answers "may this account change a record of this type?" and
gates every write. Collapsing them into one boolean is how a hostel warden
ends up approving academic leave.

**What's still a training wheel, stated plainly.** Demo credentials are
seeded with published passwords, because a project nobody can log into is
not a demo. There is no password rotation, no lockout, and the cookie is not
Secure-flagged because the demo runs on http://localhost. Each of those is a
deliberate omission with a known name.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from . import backends, config, users

# scrypt cost. n=2**14 keeps a login around a few tens of milliseconds —
# slow enough to be worth attacking properly, fast enough not to stall a demo.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32

SESSION_COOKIE = "compass_session"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: bytes | None = None) -> str:
    """Encode as scrypt$n$r$p$salt$hash — self-describing, so the cost
    parameters can change later without invalidating existing hashes."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN
    )
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(dk).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time comparison. Returns False on any malformed hash rather
    than raising — a corrupt credentials file must fail closed, not 500."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(hash_b64))


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def authenticate(username: str, password: str) -> dict | None:
    """Return the user record for a valid login, else None.

    Deliberately gives no hint about *which* half was wrong, and runs the
    hash comparison even for an unknown username so that a missing account
    and a wrong password take about the same time. A store that cannot be
    reached fails closed: nobody signs in, rather than everybody.
    """
    res = backends.get_backend().get_credentials((username or "").strip().lower())
    entry = res["data"] if res["success"] else None
    encoded = entry["password_hash"] if entry else hash_password("_no_such_user_")
    if not verify_password(password or "", encoded):
        return None
    return users.get(entry["user_id"]) if entry else None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_session(user_id: str, ttl: int | None = None) -> str:
    """A signed token: base64(payload).base64(hmac-sha256).

    Not a JWT, and not pretending to be one — no alg field means no
    algorithm-confusion attack, and this needs exactly one algorithm.
    """
    ttl = ttl if ttl is not None else config.SESSION_TTL
    payload = json.dumps(
        {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + ttl,
         "sid": secrets.token_urlsafe(12)},
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(config.session_secret(), payload, hashlib.sha256).digest()
    return f"{_b64u(payload)}.{_b64u(sig)}"


def read_session(token: str | None) -> dict | None:
    """Verify a token and return the user, or None.

    Every failure — tampered signature, expired, malformed, unknown user —
    lands in the same place: anonymous. There is no partial trust.
    """
    if not token or "." not in token:
        return None
    raw_payload, raw_sig = token.rsplit(".", 1)
    try:
        payload = _unb64u(raw_payload)
        sig = _unb64u(raw_sig)
    except (ValueError, TypeError):
        return None

    expected = hmac.new(config.session_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    if claims.get("sid") in _revoked():
        return None
    return users.get(claims.get("sub"))


# ---------------------------------------------------------------------------
# Signing out — the server forgets the session, not just the browser
# ---------------------------------------------------------------------------
# A signed token is valid until it expires, wherever it is copied to. So
# signing out records the session's id as revoked, until the moment it would
# have expired anyway; a copy of the cookie (a shared lab computer, a
# forwarded screenshot of devtools) stops working at once. Only that session
# ends: the same student on their phone stays signed in. The list is kept in
# a file so a restart doesn't forgive it, and pruned of expired entries.
_REVOKED_LOCK = threading.Lock()
_revoked_cache: dict[str, int] | None = None


def _revoked_path():
    return config.DATA_DIR / ".revoked_sessions.json"


def _revoked() -> dict[str, int]:
    global _revoked_cache
    if _revoked_cache is None:
        try:
            _revoked_cache = json.loads(_revoked_path().read_text())
        except (OSError, ValueError):
            _revoked_cache = {}
    return _revoked_cache


def revoke_session(token: str | None) -> bool:
    """Forget this session server-side. False if the token isn't a valid one."""
    if not token or "." not in token or read_session(token) is None:
        return False
    claims = json.loads(_unb64u(token.rsplit(".", 1)[0]))
    if not claims.get("sid"):
        return False
    now = int(time.time())
    with _REVOKED_LOCK:
        live = {sid: exp for sid, exp in _revoked().items() if exp > now}
        live[claims["sid"]] = int(claims["exp"])
        global _revoked_cache
        _revoked_cache = live
        try:
            tmp = _revoked_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(live))
            os.replace(tmp, _revoked_path())
        except OSError:
            pass  # still revoked in this process; a restart would forget it
    return True


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
# What each staff role may ACT on, by record type. This is the whole rule.
#
# The rung that matters is the middle one. Student-versus-staff is the
# refusal every project can show. Staff-versus-staff — a warden who is
# genuinely staff, can read every record, and still cannot approve academic
# leave — is what says an institution was modelled rather than a boolean.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "student": frozenset(),
    "hostel_warden": frozenset({"facility_complaint"}),
    "faculty_advisor": frozenset({"od_request"}),
}


def capabilities(user: dict | None) -> frozenset[str]:
    """The record types this user may act on. Empty for students."""
    return ROLE_CAPABILITIES.get((user or {}).get("role"), frozenset())


def may_action(user: dict | None, record_type: str | None) -> bool:
    """The single definition of "may act on someone else's <record type>".

    Chat and the staff console both reach this through
    `handlers.apply_staff_action`. One rule with several call sites beats
    several copies of an `if` that can drift apart — and drifting apart is
    exactly how a console button ends up doing what chat refuses.
    """
    return record_type in capabilities(user)


def is_staff(user: dict | None) -> bool:
    """Whether this is a staff account at all — any capability.

    Deliberately coarser than `may_action`: it answers "may this person read
    other people's records", not "may they touch this one". Chat lookups
    lean on it, because answering *where is Ravi's OD request* is a read the
    asker may not be able to act on.

    The queue is narrower on purpose. `/api/queue` scopes its payload with
    `capabilities` rather than this flag, so a warden is never handed leave
    requests to work through. Being allowed through a door is not the same
    as every drawer in the room being yours.
    """
    return bool(capabilities(user))


# Who may add, remove and confirm the College's documents — the calendar,
# syllabi, regulations. Kept apart from ROLE_CAPABILITIES on purpose: those
# are record types a person may decide, and a document is not a record
# anyone decides. The advisor owns academic documents; the warden, though
# staff, does not, and no student ever does — an upload is read by every
# student's assistant, so it is a write into everyone's answers.
DOCUMENT_MANAGERS: frozenset[str] = frozenset({"faculty_advisor"})


def may_manage_documents(user: dict | None) -> bool:
    return (user or {}).get("role") in DOCUMENT_MANAGERS


# The mess menu is hostel business: the warden keeps it, and everyone reads
# it. Like documents, it is not a record anyone decides, so it stays out of
# ROLE_CAPABILITIES. app/mess.py checks it first in every write.
MENU_MANAGERS: frozenset[str] = frozenset({"hostel_warden"})


def may_manage_menu(user: dict | None) -> bool:
    return (user or {}).get("role") in MENU_MANAGERS
