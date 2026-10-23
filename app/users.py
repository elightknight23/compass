"""The people directory — one loader, whichever backend holds it.

Both FastAPI (resolving a session's subject to a person, serving the staff
directory) and the handlers (resolving a *target* name like "Ravi" to a
record owner) need users, so neither owns the store.

find_by_name returns every match rather than picking one. A handler that
guesses which "Priya" you meant is a handler that eventually acts on the
wrong student's records; ambiguity gets surfaced to the user instead.
"""

from __future__ import annotations

from . import backends

# Loaded once per process. Not an lru_cache, deliberately: a failed load
# (database not up yet) must not be remembered as "nobody exists", or one
# slow container start would lock every account out until a restart.
_cache: dict[str, dict] | None = None


def _by_id() -> dict[str, dict]:
    global _cache
    if _cache is None:
        res = backends.get_backend().list_users()
        if not res["success"]:
            return {}
        _cache = {u["id"]: u for u in res["data"] or []}
    return _cache


def reload() -> None:
    """Forget the cached directory. For tests and the reset script."""
    global _cache
    _cache = None


def all_users() -> list[dict]:
    return list(_by_id().values())


def get(user_id: str | None) -> dict | None:
    return _by_id().get(user_id) if user_id else None


def find_by_name(text: str) -> list[dict]:
    """Users whose name matches a free-text fragment ("Ravi", "ravi kumar").

    Never used to decide *who is acting* — only who is being asked about.
    Actor identity comes from the signed session cookie, always.
    """
    needle = (text or "").strip().lower()
    if needle.endswith("'s"):
        needle = needle[:-2]
    if not needle:
        return []
    hits = [u for u in all_users() if needle == u["name"].lower()]
    if hits:
        return hits
    return [
        u
        for u in all_users()
        if needle in u["name"].lower()
        or any(part.startswith(needle) for part in u["name"].lower().split())
    ]
