"""Amend and cancel — the two mutations a student may perform on their own.

Both are gated on **ownership**, not role, and that check necessarily runs
after the read: you cannot know whose record it is without fetching it.

The behaviours pinned here each came from a real failure:

  "The water heater on my floor is broken"  -> files CMP-1004
  "make it high priority"                   -> must AMEND CMP-1004,
                                               not file a second complaint
                                               whose text is "make it high
                                               priority"

  "cancel my complaint, it's fixed now"     -> must WITHDRAW it, not tell a
                                               student to go and find the
                                               warden (cancelling your own
                                               is not a staff decision)

A bare follow-up resolves to the caller's most recent OPEN record, chosen by
code and never by the model — an id the model invents is a write to someone
else's record.

No LLM, no network.

    python scripts/test_amend.py
"""

from __future__ import annotations

from _harness import check, finish, sandbox, section

from app import handlers, users
from app.backends import blank_record
from app.state import new_state


def run(fn, uid: str, **entities) -> dict:
    st = new_state("(pre-classified)", users.get(uid))
    st["entities"] = entities
    return fn(st)


def complaint(cid, uid, status="open", created="2026-10-20", **kw):
    return blank_record(id=cid, user_id=uid, type="facility_complaint", status=status,
                        created_at=created, notes=kw.pop("notes", "a problem"),
                        category=kw.pop("category", "hostel"),
                        priority=kw.pop("priority", "medium"), **kw)


def leave(lid, uid, status="submitted", created="2026-10-20"):
    return blank_record(id=lid, user_id=uid, type="od_request", status=status,
                        created_at=created, notes="symposium", leave_kind="od",
                        start_date="2026-11-25", end_date="2026-11-27", working_days=3,
                        reason="symposium", needs_exception=False)


def main() -> int:
    section("amend: a bare follow-up means the most recent open record")
    world = [
        complaint("CMP-1001", "U001", created="2026-10-01"),
        complaint("CMP-1009", "U001", created="2026-10-20"),
        complaint("CMP-1010", "U001", created="2026-10-20"),  # ties on date: id breaks it
        complaint("CMP-1011", "U001", status="resolved", created="2026-10-22"),
        complaint("CMP-1020", "U002", created="2026-10-22"),
        leave("OD-2001", "U001"),
    ]
    backend = sandbox("json", world)
    out = run(handlers.handle_amend_record, "U001", priority="high")
    check("no id: resolves to CMP-1010 (newest open; numeric tiebreak, not string)",
          out["debug"].get("record_id") == "CMP-1010"
          and out["debug"].get("resolved_via") == "most recent open record", str(out["debug"]))
    check("  and only the priority changed",
          backend.get_record("CMP-1010")["data"]["priority"] == "high"
          and backend.get_record("CMP-1009")["data"]["priority"] == "medium")
    check("  a resolved record is never the fallback target",
          backend.get_record("CMP-1011")["data"]["priority"] == "medium")
    check("  someone else's newer complaint is never the fallback target",
          backend.get_record("CMP-1020")["data"]["priority"] == "medium")
    check("  the amendment was audited",
          [e["verb"] for e in backend.list_events("CMP-1010")["data"]] == ["amend"])

    out = run(handlers.handle_amend_record, "U001", category="lab", record_id="CMP-1001")
    check("a named id wins over the fallback",
          backend.get_record("CMP-1001")["data"]["category"] == "lab")

    out = run(handlers.handle_amend_record, "U001", reason="national-level symposium")
    check("a reason change resolves to the open leave request, not a complaint",
          out["debug"].get("record_id") == "OD-2001"
          and backend.get_record("OD-2001")["data"]["reason"] == "national-level symposium",
          str(out["debug"]))

    out = run(handlers.handle_amend_record, "U001", record_id="OD-2001", priority="low")
    check("a leave request has no priority: refused with what CAN change",
          out["debug"].get("outcome") == "not_amendable" and "reason" in out["reply"])

    out = run(handlers.handle_amend_record, "U001")
    check("nothing to change: asks what", out["debug"].get("outcome") == "needs_change")

    out = run(handlers.handle_amend_record, "U001", record_id="CMP-1011", priority="low")
    check("a resolved record cannot be amended — and says so, not 'not allowed'",
          out["debug"].get("outcome") == "terminal_state" and "already" in out["reply"])

    out = run(handlers.handle_amend_record, "U001", record_id="CMP-1020", priority="low")
    check("another student's complaint: refused, unchanged",
          out["debug"].get("authz") == "denied"
          and backend.get_record("CMP-1020")["data"]["priority"] == "medium")

    out = run(handlers.handle_amend_record, "U001", record_id="CMP-4040", priority="low")
    check("a missing id: not found, distinct from a refusal",
          out["debug"].get("outcome") == "not_found")

    sandbox("json", [complaint("CMP-1001", "U001", status="resolved")])
    out = run(handlers.handle_amend_record, "U001", priority="high")
    check("nothing open: says so rather than filing", out["debug"].get("outcome") == "needs_target")

    section("cancel: withdrawing your own")
    backend = sandbox("json", world)
    out = run(handlers.handle_cancel_record, "U001", record_id="OD-2001")
    check("a leave request you withdraw becomes 'withdrawn'",
          backend.get_record("OD-2001")["data"]["status"] == "withdrawn", out["reply"])
    out = run(handlers.handle_cancel_record, "U001", record_id="CMP-1001")
    check("a complaint you withdraw becomes 'cancelled'",
          backend.get_record("CMP-1001")["data"]["status"] == "cancelled")
    check("  both withdrawals were audited",
          backend.list_events("OD-2001")["data"][0]["verb"] == "withdraw")
    out = run(handlers.handle_cancel_record, "U001", record_id="OD-2001")
    check("withdrawing it twice: terminal, not an error",
          out["debug"].get("outcome") == "terminal_state")
    out = run(handlers.handle_cancel_record, "U001", record_id="CMP-1020")
    check("another student's: refused, unchanged",
          out["debug"].get("authz") == "denied"
          and backend.get_record("CMP-1020")["data"]["status"] == "open")

    out = run(handlers.handle_cancel_record, "U001")
    check("several open and none named: asks which, writes nothing",
          out["debug"].get("outcome") == "needs_target"
          and set(out["debug"].get("candidates", [])) == {"CMP-1009", "CMP-1010"},
          str(out["debug"]))
    out = run(handlers.handle_cancel_record, "U001", applies_to_all=True)
    check('"all of them" withdraws every open one and names each',
          set(out["debug"].get("cancelled", [])) == {"CMP-1009", "CMP-1010"}
          and backend.get_record("CMP-1020")["data"]["status"] == "open", out["reply"])
    out = run(handlers.handle_cancel_record, "U001")
    check("then there is nothing left to withdraw",
          out["debug"].get("outcome") == "nothing_to_cancel")

    backend = sandbox("json", [complaint("CMP-1001", "U002")])
    out = run(handlers.handle_cancel_record, "U002")
    check("exactly one open: withdrawn without asking",
          backend.get_record("CMP-1001")["data"]["status"] == "cancelled")

    section("filing")
    backend = sandbox("json")
    out = run(handlers.handle_file_complaint, "U002", category="library",
              description="Reading room AC is off", priority="high")
    rec = backend.get_record(out["debug"].get("record_id", ""))["data"]
    check("files with the described problem, not the raw follow-up message",
          rec and rec["notes"] == "Reading room AC is off" and rec["priority"] == "high")
    out = run(handlers.handle_file_complaint, "U002", category="transport")
    rec = backend.get_record(out["debug"].get("record_id", ""))["data"]
    check("no stated priority: code's default, medium", rec and rec["priority"] == "medium")
    out = run(handlers.handle_file_complaint, "U002")
    check("no category: asks, because the category decides whose inbox",
          out["debug"].get("outcome") == "needs_category")

    return finish("amend / cancel")


if __name__ == "__main__":
    raise SystemExit(main())
