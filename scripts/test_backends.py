"""The record protocol, run identically against every backend.

The handlers cannot tell the backends apart, and this suite is why that is a
fact rather than a hope: every check below runs against

    json       JsonBackend over a temp copy of the seed files
    sql        PostgresBackend over a temp SQLite file (always available)
    postgres   PostgresBackend over a real server, in a separate test
               database — skipped, and said so, if it is unreachable

The protocol rules pinned here are the ones the handlers lean on:
get_record on a missing id is ok(None), never an error (I6); an unknown
field is refused; immutable fields stay immutable; lists filter by type;
the directory never carries a credential; every failure is an envelope.

    python scripts/test_backends.py
    TEST_DATABASE_URL=postgresql+psycopg://... python scripts/test_backends.py
"""

from __future__ import annotations

import os

from _harness import check, finish, results, sandbox, section

from app.backends import RECORD_FIELDS

PG_ADMIN = os.getenv(
    "TEST_DATABASE_ADMIN_URL", "postgresql+psycopg://compass:compass@localhost:5433/compass"
)
PG_TEST = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg://compass:compass@localhost:5433/compass_test"
)


def postgres_test_url() -> str | None:
    """A throwaway database beside the demo one, created on first use.

    Never the demo database itself: seeding drops every table, and a test
    run must not reset the world someone is about to demo in.
    """
    try:
        from sqlalchemy import create_engine, text

        admin = create_engine(PG_ADMIN, isolation_level="AUTOCOMMIT",
                              connect_args={"connect_timeout": 3})
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'compass_test'")
            ).scalar()
            if not exists:
                conn.execute(text("CREATE DATABASE compass_test"))
        admin.dispose()
        return PG_TEST
    except Exception as e:  # noqa: BLE001
        print(f"   (postgres unreachable: {type(e).__name__} — skipping; "
              "docker compose up -d to include it)")
        return None


def protocol(kind: str) -> None:
    b = sandbox(kind)
    tag = f"[{b.name}]"

    r = b.get_record("CMP-1001")
    check(f"{tag} get_record returns a full record",
          r["success"] and set(r["data"]) == set(RECORD_FIELDS), str(r)[:120])
    check(f"{tag} ids are case-insensitive", b.get_record("cmp-1001")["data"] is not None)
    r = b.get_record("CMP-4040")
    check(f"{tag} a missing id is ok(None), not an error (I6)",
          r["success"] is True and r["data"] is None, str(r))

    r = b.create_complaint("U002", "Lab 4 projector flickers", "lab", "high")
    new_id = (r["data"] or {}).get("id")
    check(f"{tag} create_complaint allocates the next id", new_id == "CMP-1004", str(new_id))
    check(f"{tag}   status open, created today",
          r["data"]["status"] == "open" and r["data"]["created_at"] == "2026-10-23")
    r = b.create_od_request("U001", "od", "2026-11-25", "2026-11-27", 3, "symposium",
                            {"current_pct": 0.76, "projected_pct": 0.77}, False)
    check(f"{tag} create_od_request allocates the next id and keeps the projection",
          r["data"]["id"] == "OD-2004" and r["data"]["projection"]["projected_pct"] == 0.77
          and r["data"]["status"] == "submitted" and r["data"]["needs_exception"] is False,
          str(r["data"])[:160])

    r = b.update_record("CMP-1004", priority="low", status="resolved")
    check(f"{tag} update_record patches mutable fields",
          r["success"] and r["data"]["priority"] == "low" and r["data"]["status"] == "resolved")
    check(f"{tag}   and the change persists",
          b.get_record("CMP-1004")["data"]["status"] == "resolved")
    r = b.update_record("CMP-1004", user_id="U003")
    check(f"{tag} the owner is immutable", not r["success"] and r["kind"] == "invalid")
    r = b.update_record("OD-2004", start_date="2026-12-01")
    check(f"{tag} leave dates are immutable (new dates = new request)",
          not r["success"] and b.get_record("OD-2004")["data"]["start_date"] == "2026-11-25")
    r = b.update_record("CMP-4040", status="resolved")
    check(f"{tag} updating a missing record is not_found", r["kind"] == "not_found")

    mine = b.list_records_for_user("U001")["data"]
    check(f"{tag} list_records_for_user returns only theirs",
          mine and all(x["user_id"] == "U001" for x in mine))
    only = b.list_records_for_user("U001", ("od_request",))["data"]
    check(f"{tag}   and filters by type", only and all(x["type"] == "od_request" for x in only))
    allr = b.list_all_records(("facility_complaint",))["data"]
    check(f"{tag} list_all_records filters by type",
          allr and {x["type"] for x in allr} == {"facility_complaint"})
    dates = [x["created_at"] for x in b.list_all_records()["data"]]
    check(f"{tag}   newest first", dates == sorted(dates, reverse=True), str(dates))

    e = b.append_event("OD-2004", "U102", "approve", "submitted", "approved", "note")
    check(f"{tag} append_event records who did what",
          e["success"] and e["data"]["actor_id"] == "U102" and e["data"]["at"])
    b.append_event("OD-2004", "U001", "amend", "submitted", "submitted")
    check(f"{tag} list_events is oldest first and per record",
          [x["verb"] for x in b.list_events("OD-2004")["data"]] == ["approve", "amend"]
          and b.list_events("CMP-1001")["data"] == [])

    people = b.list_users()["data"]
    check(f"{tag} list_users returns the directory",
          {u["id"] for u in people} == {"U001", "U002", "U003", "U101", "U102"})
    check(f"{tag}   without any credential",
          all("password" not in u and "password_hash" not in u for u in people))
    cred = b.get_credentials("lakshmi.narayanan")["data"]
    check(f"{tag} get_credentials returns the hash for login",
          cred["user_id"] == "U102" and cred["password_hash"].startswith("scrypt$"))
    check(f"{tag} an unknown username is ok(None)",
          b.get_credentials("nobody")["data"] is None)

    att = b.get_attendance("U001")["data"]
    check(f"{tag} get_attendance", att == {"attended": 457, "held": 600, "as_of": "2026-10-21"})
    check(f"{tag} no attendance on file is ok(None)", b.get_attendance("U101")["data"] is None)


def main() -> int:
    section("json")
    protocol("json")
    section("sql (SQLite)")
    protocol("sql")
    section("postgres")
    url = postgres_test_url()
    if url:
        protocol(url)

    section("failure is an envelope, never an exception")
    from app.backends.postgres_backend import PostgresBackend

    dead = PostgresBackend("postgresql+psycopg://x:y@127.0.0.1:1/nope")
    r = dead.get_record("OD-2001")
    check("an unreachable database returns fail(unavailable)",
          r["success"] is False and r["kind"] == "unavailable", str(r))
    check("  and the error does not leak the connection URL", "x:y" not in (r["error"] or ""))
    check("  writes fail the same way",
          dead.create_complaint("U001", "x", "lab", "low")["kind"] == "unavailable")

    if not url:
        print(f"\n(postgres was skipped: {len(results)} checks ran without it)")
    return finish("backends")


if __name__ == "__main__":
    raise SystemExit(main())
