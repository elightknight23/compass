"""Pluggable record backends — the swap boundary.

Handlers never import a concrete backend. They ask for one here, so "how I
get the data" stays separate from "what I do with it".

    RECORD_BACKEND=postgres|json   # .env, read once at startup

Postgres is the system of record. JSON is the disaster plan: if the
database is unreachable during a demo, one environment variable flips the
whole system to local files and everything keeps working, because both
satisfy the same protocol and the handlers cannot tell them apart.

Both record types — facility complaints and OD/leave requests — live behind
the one protocol. (The predecessor kept leave out of its ticket protocol,
because an external issue tracker had nowhere to put dates. With a real
database underneath, that asymmetry has nothing left to protect. If an
external tracker is ever added for complaints, leave still must not go
into it.)

Every method returns a result envelope rather than raising, so a failure
reaches the user as "I couldn't reach the records system", never as a
stack trace and never as a fabricated record id.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from .. import config

COMPLAINT_PREFIX = "CMP-"
OD_PREFIX = "OD-"

COMPLAINT = "facility_complaint"
OD_REQUEST = "od_request"

# Every record carries every key, so a consumer never has to ask which type
# it holds before reading a field. The type-specific ones are None on the
# other type.
RECORD_FIELDS = (
    "id", "user_id", "type", "status", "created_at", "notes",
    # facility_complaint
    "category", "priority",
    # od_request
    "leave_kind", "start_date", "end_date", "working_days", "reason",
    "projection", "needs_exception",
    # set when a staff decision lands
    "decided_by", "decided_at",
)

# Fields a caller may patch. id, user_id, type and created_at are immutable
# once written — and so are the dates and the projection: changing the dates
# of a leave request means a new projection, which means a new request.
MUTABLE_FIELDS = frozenset(
    {"status", "category", "priority", "notes", "reason", "decided_by", "decided_at"}
)

# Failure kinds. "the record doesn't exist" and "the system is down" are
# different sentences to the user, so the envelope keeps them apart.
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
INVALID = "invalid"


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------
def ok(data: Any = None, **extra: Any) -> dict:
    """A successful call. `data` may legitimately be None (see get_record)."""
    return {"success": True, "data": data, "error": None, "kind": None, **extra}


def fail(error: str, kind: str = UNAVAILABLE, **extra: Any) -> dict:
    """A failed call. `error` is human-readable — it may reach the user."""
    return {"success": False, "data": None, "error": error, "kind": kind, **extra}


def blank_record(**fields: Any) -> dict:
    """A record with every key present, the given ones filled in."""
    return {k: fields.get(k) for k in RECORD_FIELDS}


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class RecordBackend(Protocol):
    """The contract every store satisfies.

    Implementations must not raise for expected failures (missing record,
    connection refused, timeout) — they return fail(...) instead.

    Note what is *absent*: no permission checks. Authorization is enforced
    in the handlers against the session-resolved user, not down here where
    a caller could skip it by picking a different backend. Every list method
    returns everything it can see; the caller decides who may look.
    """

    name: str

    # -- records -----------------------------------------------------------
    def create_complaint(
        self, user_id: str, notes: str, category: str, priority: str
    ) -> dict:
        """Open a facility complaint. ok(record) with a real id, or fail(...)."""
        ...

    def create_od_request(
        self,
        user_id: str,
        leave_kind: str,
        start_date: str,
        end_date: str,
        working_days: int,
        reason: str,
        projection: dict | None,
        needs_exception: bool,
    ) -> dict:
        """Submit an OD/leave request with its attendance snapshot attached."""
        ...

    def get_record(self, record_id: str) -> dict:
        """Fetch one record. ok(None) means "no such record" — a successful
        query with an empty answer — while fail(...) means the store could
        not be reached. Conflating those two is how agents start lying."""
        ...

    def update_record(self, record_id: str, **fields: Any) -> dict:
        """Patch MUTABLE_FIELDS. ok(updated_record) or fail(...)."""
        ...

    def list_records_for_user(
        self, user_id: str, types: tuple[str, ...] | None = None
    ) -> dict:
        """One person's records, optionally of some types. ok(list)."""
        ...

    def list_all_records(self, types: tuple[str, ...] | None = None) -> dict:
        """Everyone's records, newest first, for the staff queue. ok(list)."""
        ...

    # -- audit trail -------------------------------------------------------
    def append_event(
        self,
        record_id: str,
        actor_id: str,
        verb: str,
        from_status: str | None,
        to_status: str | None,
        note: str | None = None,
    ) -> dict:
        """Record who did what to a record. ok(event) or fail(...)."""
        ...

    def list_events(self, record_id: str) -> dict:
        """A record's history, oldest first. ok(list)."""
        ...

    # -- people and attendance --------------------------------------------
    def list_users(self) -> dict:
        """The directory, without credentials. ok(list)."""
        ...

    def get_credentials(self, username: str) -> dict:
        """ok({"user_id", "password_hash"}) or ok(None) for an unknown name."""
        ...

    def get_attendance(self, user_id: str) -> dict:
        """ok({"attended", "held", "as_of"}) or ok(None) if none is on file."""
        ...


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_backend() -> RecordBackend:
    """The env-selected backend, resolved once per process.

    Read through `config`, not `os.getenv`, and that is load-bearing rather
    than tidiness: `config` is where `load_dotenv()` runs, so reading the
    environment directly here would make the answer depend on import order
    — a script that reached this module first would silently get a
    different store from the one .env names.
    """
    choice = (config.RECORD_BACKEND or "json").strip().lower()
    if choice == "json":
        from .json_backend import JsonBackend  # lazy: it imports from here

        return JsonBackend()
    if choice == "postgres":
        from .postgres_backend import PostgresBackend

        return PostgresBackend()
    raise ValueError(f"Unknown RECORD_BACKEND={choice!r}. Expected postgres or json.")
