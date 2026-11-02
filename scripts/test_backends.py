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
    ravi = next(u for u in people if u["id"] == "U001")
    check(f"{tag} the directory carries branch and semester (they filter the syllabus)",
          (ravi.get("branch"), ravi.get("semester")) == ("CSE", 5), str(ravi)[:120])

    # -- uploaded documents and their calendar ----------------------------
    check(f"{tag} no documents to begin with", b.list_documents()["data"] == [])
    doc = dict(title="Academic Calendar 2026-27", filename="cal.pdf", content_type="application/pdf",
               sha256="ab" * 32, size=1234, pages=None, doc_type="academic_calendar", branch="ALL",
               semester=None, academic_year="2026-27", status="processing", error=None, chunks=None,
               uploaded_by="U102", uploaded_at="2026-10-23T10:00:00+00:00")
    d1 = b.create_document(**doc)["data"]
    d2 = b.create_document(**{**doc, "title": "CSE Syllabus", "sha256": "cd" * 32,
                              "doc_type": "syllabus", "branch": "CSE",
                              "uploaded_at": "2026-10-23T11:00:00+00:00"})["data"]
    check(f"{tag} create_document allocates DOC- ids in order",
          (d1["id"], d2["id"]) == ("DOC-3001", "DOC-3002"), f"{d1['id']} {d2['id']}")
    check(f"{tag} get_document is case-insensitive and full",
          b.get_document("doc-3001")["data"]["title"] == "Academic Calendar 2026-27")
    r = b.get_document("DOC-9999")
    check(f"{tag} a missing document is ok(None) (I6)", r["success"] and r["data"] is None)
    check(f"{tag} list_documents is newest first",
          [d["id"] for d in b.list_documents()["data"]] == ["DOC-3002", "DOC-3001"])
    r = b.update_document("DOC-3001", status="needs_review", chunks=12, pages=2)
    check(f"{tag} update_document moves the processing outcome",
          r["success"] and b.get_document("DOC-3001")["data"]["chunks"] == 12)
    r = b.update_document("DOC-3001", branch="ECE")
    check(f"{tag} a document's branch is immutable (it is baked into the index)",
          not r["success"] and r["kind"] == "invalid")
    check(f"{tag} updating a missing document is not_found",
          b.update_document("DOC-9999", status="indexed")["kind"] == "not_found")

    events = [
        dict(start_date="2026-11-18", end_date="2026-11-18", title="Founders' Day", kind="holiday",
             years="all", source_text="18.11.2026 Wednesday Founders' Day", page=1, flags=[],
             include=True, confirmed=False),
        dict(start_date="2026-11-09", end_date="2026-11-11", title="Deepavali", kind="holiday",
             years="all", source_text="09.11.2026 to 11.11.2026 Deepavali", page=1,
             flags=["inferred_year"], include=True, confirmed=False),
    ]
    stored = b.replace_calendar_events("DOC-3001", events)["data"]
    check(f"{tag} replace_calendar_events stores them with ids and the document",
          len(stored) == 2 and all(e["id"] and e["document_id"] == "DOC-3001" for e in stored))
    check(f"{tag} unconfirmed events are not 'in force'",
          b.list_calendar_events()["data"] == [])
    drafts = b.list_calendar_events(confirmed_only=False, document_id="DOC-3001")["data"]
    check(f"{tag} drafts come back ordered by date, flags intact",
          [e["title"] for e in drafts] == ["Deepavali", "Founders' Day"]
          and drafts[0]["flags"] == ["inferred_year"])
    b.replace_calendar_events("DOC-3001", events + [dict(events[0], start_date=None, end_date=None,
                                                          title="Pongal (TBA)")])
    drafts = b.list_calendar_events(confirmed_only=False)["data"]
    check(f"{tag} an undated row sorts last", drafts[-1]["title"] == "Pongal (TBA)", str([e["title"] for e in drafts]))
    b.replace_calendar_events("DOC-3001", [{**e, "confirmed": True} for e in events[:1]])
    live = b.list_calendar_events()["data"]
    check(f"{tag} replacing is a replace, not an append; confirmed ones are in force",
          [e["title"] for e in live] == ["Founders' Day"]
          and len(b.list_calendar_events(confirmed_only=False)["data"]) == 1)

    gone = b.delete_document("DOC-3001")
    check(f"{tag} delete_document returns what it removed",
          gone["success"] and gone["data"]["id"] == "DOC-3001")
    check(f"{tag}   and its calendar events went with it",
          b.list_calendar_events(confirmed_only=False)["data"] == [])
    check(f"{tag}   leaving the other document alone",
          [d["id"] for d in b.list_documents()["data"]] == ["DOC-3002"])
    check(f"{tag} deleting a missing document is not_found",
          b.delete_document("DOC-3001")["kind"] == "not_found")

    # -- the mess menu -----------------------------------------------------
    check(f"{tag} list_menus starts empty (the sample is app/mess.py's, not stored)",
          b.list_menus() == {**b.list_menus(), "success": True} and b.list_menus()["data"] == [])
    week = {"monday": {"lunch": ["Rice", "Dal"]}}
    saved = b.save_menu("IFC B", {"week": week, "timings": {"lunch": "12:30"}, "updated_by": "U101",
                                  "updated_at": "2026-10-23T09:00:00+00:00"})
    check(f"{tag} save_menu stores one mess's week, and returns it",
          saved["success"] and saved["data"]["mess"] == "IFC B" and saved["data"]["week"] == week)
    b.save_menu("IFC B", {"week": {"monday": {"lunch": ["Biryani"]}}, "timings": {}, "updated_by": "U101",
                          "updated_at": "2026-10-24T09:00:00+00:00"})
    b.save_menu("IFC A", {"week": week, "timings": {}, "updated_by": "U101", "updated_at": None})
    menus = {m["mess"]: m for m in b.list_menus()["data"]}
    check(f"{tag}   saving again replaces it, and other messes are kept",
          sorted(menus) == ["IFC A", "IFC B"] and menus["IFC B"]["week"]["monday"]["lunch"] == ["Biryani"]
          and menus["IFC B"]["updated_at"].startswith("2026-10-24"), str(menus.get("IFC B")))

    if kind != "json":
        # The demo reset re-seeds Postgres; the College's documents survive it.
        from seed_db import seed

        b.replace_calendar_events("DOC-3002", [dict(events[0], confirmed=True)])
        seed(b.url, b._sandbox_dir, keep_documents=True)
        check(f"{tag} a records reset keeps documents and their reviewed calendar",
              [d["id"] for d in b.list_documents()["data"]] == ["DOC-3002"]
              and len(b.list_calendar_events()["data"]) == 1
              and b.get_record("CMP-1004")["data"] is None)
        seed(b.url, b._sandbox_dir)
        check(f"{tag} a fresh seed does not", b.list_documents()["data"] == [])


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
