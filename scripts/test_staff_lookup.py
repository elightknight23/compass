"""Who may act on what — the full role × record type × verb matrix.

Every staff mutation goes through handlers.apply_staff_action, from chat
and from the console alike, so this suite drives it directly with every
combination and asserts two things each time: the outcome, and — for every
refusal — that the record did not move. A refusal that still wrote the row
is the bug this project most wants not to have.

It also pins the order of the checks (README, invariant 3). The interesting cell is
**the advisor trying to approve a complaint**. The advisor lacks the
complaint capability AND approve does not apply to complaints. The correct
answer is the type error — "a complaint can't be approved" is true whoever
asks — and it is only reachable because the type check runs before the
capability check. Swap them and that cell turns into not_authorized, and the
type guard becomes dead code for every real role.

Then the chat path: name resolution ("approve Ravi's"), remit refusals
before lookup, and the two predecessor bugs this port fixed — staff reading
someone else's records got their own, and staff could cancel a student's
request through the owner-only path.

No LLM, no network. Entities are pre-filled exactly as the classifier would.

    python scripts/test_staff_lookup.py
"""

from __future__ import annotations

from _harness import check, finish, sandbox, section

from app import handlers, users
from app.state import new_state

ROLES = {"student": "U001", "hostel_warden": "U101", "faculty_advisor": "U102"}
RECORDS = {"facility_complaint": "CMP-1001", "od_request": "OD-2001"}  # both open, both Ravi's
VERBS = ("approve", "reject", "resolve", "cancel")
WRITES = {"approve": "approved", "reject": "rejected", "resolve": "resolved", "cancel": "cancelled"}
APPLIES = {
    "approve": {"od_request"},
    "reject": {"od_request"},
    "resolve": {"facility_complaint"},
    "cancel": {"od_request", "facility_complaint"},
}
CAN = {"student": set(), "hostel_warden": {"facility_complaint"},
       "faculty_advisor": {"od_request"}}


def expected(role: str, rtype: str, verb: str) -> str:
    """The rule, restated independently of the implementation, in I3 order."""
    if not CAN[role]:
        return "not_authorized"         # 1. students, before any read
    if rtype not in APPLIES[verb]:
        return "wrong_action_for_type"  # 2. verb against type
    if rtype not in CAN[role]:
        return "not_authorized"         # 3. capability against type
    return "updated"


def chat(fn, uid: str, **entities) -> dict:
    st = new_state("(pre-classified)", users.get(uid))
    st["entities"] = entities
    return fn(st)


def main() -> int:
    section("the matrix: 3 roles x 2 record types x 4 verbs")
    for role, uid in ROLES.items():
        for rtype, rid in RECORDS.items():
            for verb in VERBS:
                backend = sandbox("json")
                before = backend.get_record(rid)["data"]["status"]
                res = handlers.apply_staff_action(users.get(uid), rid, verb)
                after = backend.get_record(rid)["data"]["status"]
                want = expected(role, rtype, verb)
                got = res.get("outcome")
                label = f"{role:<15} {verb:<7} {rtype:<18} -> {want}"
                if want == "updated":
                    check(label, got == "updated" and after == WRITES[verb],
                          f"got {got}, status {after}")
                else:
                    check(label, got == want and after == before and not res["success"],
                          f"got {got}, status {before} -> {after}")

    section("check order (I3)")
    sandbox("json")
    res = handlers.apply_staff_action(users.get("U102"), "CMP-1001", "approve")
    check("advisor approving a complaint is a TYPE error, not a permission error",
          res.get("outcome") == "wrong_action_for_type", str(res))
    res = handlers.apply_staff_action(users.get("U101"), "OD-2001", "resolve")
    check("warden resolving a leave request is a TYPE error too",
          res.get("outcome") == "wrong_action_for_type", str(res))
    res = handlers.apply_staff_action(users.get("U001"), "NO-SUCH", "approve")
    check("a student is refused before any read — even for a record that doesn't exist",
          res.get("outcome") == "not_authorized", str(res))
    res = handlers.apply_staff_action(None, "OD-2001", "approve")
    check("no user at all is refused the same way", res.get("outcome") == "not_authorized")
    res = handlers.apply_staff_action(users.get("U101"), "OD-2001", "approve")
    check("the capability refusal names who CAN decide it",
          "faculty advisor" in res["error"] and "Lakshmi" in res["error"], res["error"])

    section("refusals are distinguishable")
    sandbox("json")
    advisor, warden = users.get("U102"), users.get("U101")
    res = handlers.apply_staff_action(advisor, "OD-9999", "approve")
    check("missing record -> not_found", res.get("outcome") == "not_found")
    res = handlers.apply_staff_action(advisor, "OD-2003", "reject")
    check("already-approved record -> terminal_state",
          res.get("outcome") == "terminal_state" and "already" in res["error"], str(res))
    res = handlers.apply_staff_action(warden, "CMP-1002", "resolve")
    check("already-resolved complaint -> terminal_state", res.get("outcome") == "terminal_state")
    res = handlers.apply_staff_action(advisor, "OD-2001", "escalate")
    check("unknown verb -> unknown_action", res.get("outcome") == "unknown_action")

    section("the exception gate")
    backend = sandbox("json")
    res = handlers.apply_staff_action(advisor, "OD-2002", "approve")
    check("approving a flagged request without the exception is refused",
          res.get("outcome") == "needs_exception_ack"
          and backend.get_record("OD-2002")["data"]["status"] == "submitted", str(res))
    check("  and the refusal carries the numbers",
          "73.0%" in res["error"] and "70.9%" in res["error"], res["error"])
    res = handlers.apply_staff_action(advisor, "OD-2002", "reject")
    check("rejecting a flagged request needs no exception", res.get("outcome") == "updated")
    backend = sandbox("json")
    res = handlers.apply_staff_action(advisor, "OD-2002", "approve", grant_exception=True)
    rec = backend.get_record("OD-2002")["data"]
    check("with grant_exception it is approved", rec["status"] == "approved")
    check("  and the decision is stamped with who and when",
          rec["decided_by"] == "U102" and rec["decided_at"] == "2026-10-23")
    events = backend.list_events("OD-2002")["data"]
    check("  and the audit trail says an exception was granted",
          len(events) == 1 and events[0]["note"] == "attendance exception granted", str(events))
    res = handlers.apply_staff_action(warden, "OD-2002", "approve", grant_exception=True)
    check("grant_exception never widens a capability (warden still refused)",
          res.get("outcome") == "not_authorized")

    section("chat: staff_action")
    backend = sandbox("json")
    out = chat(handlers.handle_staff_action, "U102", action="approve", target_person="Ravi")
    check('"approve Ravi\'s OD" resolves to OD-2001 and approves it',
          out["debug"].get("record_id") == "OD-2001"
          and backend.get_record("OD-2001")["data"]["status"] == "approved", out["reply"])
    out = chat(handlers.handle_staff_action, "U101", action="resolve", target_person="Arjun")
    check('"resolve Arjun\'s complaint" resolves to CMP-1003',
          out["debug"].get("record_id") == "CMP-1003", out["reply"])
    out = chat(handlers.handle_staff_action, "U102", action="approve", target_person="Priya")
    check("a name with nothing pending says so, about the right thing",
          out["debug"].get("outcome") == "needs_target" and "pending leave" in out["reply"],
          out["reply"])
    out = chat(handlers.handle_staff_action, "U101", action="approve", target_person="Ravi")
    check("the warden asking to approve is refused BEFORE the lookup",
          out["debug"].get("outcome") == "not_authorized" and "record_id" not in out["debug"],
          str(out["debug"]))
    out = chat(handlers.handle_staff_action, "U001", action="approve", target_person="Ravi")
    check("a student asking to approve their own request is refused",
          out["debug"].get("authz") == "denied")
    out = chat(handlers.handle_staff_action, "U102", target_person="Ravi")
    check("no verb heard: it asks, rather than assuming approve",
          out["debug"].get("outcome") == "needs_action")
    out = chat(handlers.handle_staff_action, "U102", action="approve", target_person="Zed")
    check("an unknown name is reported, not guessed", "couldn't find" in out["reply"])
    out = chat(handlers.handle_staff_action, "U102", action="approve", record_id="OD-2002")
    check("chat explains the exception and how to grant it",
          "as an exception" in out["reply"], out["reply"])
    out = chat(handlers.handle_staff_action, "U102", action="approve", record_id="OD-2002",
               grant_exception=True)
    check('  and "approve it as an exception" approves it',
          backend.get_record("OD-2002")["data"]["status"] == "approved", out["reply"])

    section('chat: "yes, approve it as an exception" with no id or name')
    backend = sandbox("json")
    asked = chat(handlers.handle_staff_action, "U102", action="approve", target_person="Arjun")["reply"]

    def follow_up(uid: str, last_reply: str) -> dict:
        st = new_state("Yes, approve it as an exception", users.get(uid),
                       history=[{"role": "user", "content": "Approve Arjun's leave request"},
                                {"role": "assistant", "content": last_reply}])
        st["entities"] = {"action": "approve", "grant_exception": True}
        return handlers.handle_staff_action(st)

    out = follow_up("U102", asked)
    check("the record named in Compass's last reply is the one approved",
          out["debug"].get("record_id") == "OD-2002"
          and backend.get_record("OD-2002")["data"]["status"] == "approved", out["reply"])
    backend = sandbox("json")
    out = follow_up("U101", asked)
    check("  and it widens nothing: the warden is still refused",
          out["debug"].get("authz") == "denied"
          and backend.get_record("OD-2002")["data"]["status"] == "submitted", out["reply"])
    out = follow_up("U102", "OD-2001 and OD-2002 are both waiting.")
    check("two ids in the last reply: it asks, rather than picking one",
          out["debug"].get("outcome") == "needs_target")
    st = new_state("approve it", users.get("U102"),
                   history=[{"role": "assistant", "content": asked}])
    st["entities"] = {"action": "approve"}
    out = handlers.handle_staff_action(st)
    check("without an explicit exception, no id is taken from the conversation",
          out["debug"].get("outcome") == "needs_target")

    section("chat: reading other people's records")
    sandbox("json")
    out = chat(handlers.handle_check_status, "U101", target_person="Arjun")
    check("staff naming a student get THAT student's records (predecessor bug: got their own)",
          out["debug"].get("subject") == "U003" and "CMP-1003" in out["reply"], out["reply"])
    out = chat(handlers.handle_check_status, "U001", target_person="Arjun")
    check("a student naming someone else is refused",
          out["debug"].get("authz") == "denied" and "CMP-1003" not in out["reply"])
    out = chat(handlers.handle_check_status, "U001", record_id="OD-2002")
    check("a student asking for someone else's record by id is refused",
          out["debug"].get("authz") == "denied")
    out = chat(handlers.handle_check_status, "U101", record_id="OD-2002")
    check("the warden may READ a leave request (is_staff) though never act on it",
          out["debug"].get("outcome") == "found")
    out = chat(handlers.handle_check_status, "U102", target_person="Arjun", about_attendance=True)
    check("staff can read a student's attendance", "73.0%" in out["reply"], out["reply"])
    out = chat(handlers.handle_check_status, "U002", target_person="Arjun", about_attendance=True)
    check("a student cannot read another student's attendance",
          out["debug"].get("authz") == "denied" and "73.0%" not in out["reply"])

    section("the owner-only path cannot be used by staff (predecessor bug)")
    backend = sandbox("json")
    out = chat(handlers.handle_cancel_record, "U101", record_id="OD-2001")
    check("the warden cannot withdraw Ravi's leave request through cancel_record",
          out["debug"].get("authz") == "denied"
          and backend.get_record("OD-2001")["data"]["status"] == "submitted", out["reply"])
    out = chat(handlers.handle_amend_record, "U102", record_id="CMP-1001", priority="low")
    check("the advisor cannot amend Ravi's complaint through amend_record",
          out["debug"].get("authz") == "denied"
          and backend.get_record("CMP-1001")["data"]["priority"] == "high")

    return finish("staff lookup / RBAC")


if __name__ == "__main__":
    raise SystemExit(main())
