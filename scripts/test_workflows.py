"""Workflows: what happens across many turns, by several people, in any order.

The other suites check each action once, from a clean seed. The bugs this
one exists for only appear in a sequence: two one-day requests that each
"keep you above 75%", a request re-sent while the reply was slow, a staff
member naming a student who has two requests pending.

Two parts:

1. **Journeys.** Scripted stories across accounts, each one a regression for
   a bug found by playing the app as its users would.

2. **Random walks.** Seeded random sequences of every mutation (filing,
   leave, amend, withdraw, certificates, staff actions over HTTP and chat)
   by every account, against a reference model of the rules written
   independently of the handlers. After every step the store must match the
   model exactly, and a refused step must leave no trace. The seed is
   printed, so a failure replays:

       WORKFLOW_SEED=1234 python scripts/test_workflows.py
       WORKFLOW_WALKS=200 python scripts/test_workflows.py   # longer soak
"""

from __future__ import annotations

import os
import random
from datetime import date, timedelta

from _harness import check, finish, sandbox, section
from fastapi.testclient import TestClient

from app import clock, dates, handlers, users
from app.main import app
from app.state import new_state

PASSWORDS = {"U001": ("ravi.kumar", "ravi-demo"), "U002": ("priya.sharma", "priya-demo"),
             "U003": ("arjun.menon", "arjun-demo"), "U101": ("meena.krishnan", "warden-demo"),
             "U102": ("lakshmi.narayanan", "advisor-demo")}
STUDENTS, WARDEN, ADVISOR = ("U001", "U002", "U003"), "U101", "U102"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def chat(fn, uid: str, message: str = "(pre-classified)", history=None, **entities) -> dict:
    st = new_state(message, users.get(uid), history=history or [])
    st["entities"] = entities
    return fn(st)


def login(uid: str) -> TestClient:
    c = TestClient(app)
    username, password = PASSWORDS[uid]
    assert c.post("/api/login", json={"username": username, "password": password}).status_code == 200
    return c


def leave(uid: str, kind: str, start: str, end: str | None = None) -> dict:
    return chat(handlers.handle_request_od_leave, uid, leave_kind=kind,
                start_date=start, end_date=end or start)


# ---------------------------------------------------------------------------
# Part 1: journeys
# ---------------------------------------------------------------------------
def journeys() -> None:
    section("stacking: separate requests add up")
    backend = sandbox("json")
    # Ravi: 457/600, one day of margin. Today is Fri 23 Oct.
    a = leave("U001", "medical", "2026-11-02")
    check("one day of medical leave alone keeps Ravi above 75%",
          a["debug"]["outcome"] == "created" and a["debug"]["needs_exception"] is False, a["reply"])
    b = leave("U001", "medical", "2026-11-03")
    check("a second, separate day is flagged: together they cross the line",
          b["debug"]["outcome"] == "created" and b["debug"]["needs_exception"] is True, b["reply"])
    check("  and the sentence names the leave it counted",
          a["debug"]["record_id"] in b["reply"] and "below the 75%" in b["reply"], b["reply"])
    check("  its stored projection carries the other day",
          backend.get_record(b["debug"]["record_id"])["data"]["projection"]["other_absent_days"] == 1)
    c = leave("U001", "family_emergency", "2026-11-05")
    check("family-emergency leave stacks the same way",
          c["debug"]["needs_exception"] is True
          and c["debug"]["other_leave_counted"] == {a["debug"]["record_id"]: 1, b["debug"]["record_id"]: 1},
          str(c["debug"].get("other_leave_counted")))
    d = leave("U001", "od", "2026-11-10")
    check("OD still lifts the figure, but counts the absences already on file",
          d["debug"]["attendance"]["other_absent_days"] == 3
          and d["debug"]["attendance"]["projected_pct"] > (457 + 6) / (600 + 6 * 4) - 1e-9,
          str(d["debug"]["attendance"]))

    section("stacking: withdrawn, rejected and OD leave don't count against you")
    sandbox("json")
    a = leave("U001", "medical", "2026-11-02")
    chat(handlers.handle_cancel_record, "U001", record_id=a["debug"]["record_id"])
    b = leave("U001", "medical", "2026-11-03")
    check("after withdrawing the first, the second is judged on its own",
          b["debug"]["needs_exception"] is False and "other leave" not in b["reply"], b["reply"])
    handlers.apply_staff_action(users.get(ADVISOR), b["debug"]["record_id"], "reject")
    c = leave("U001", "medical", "2026-11-04")
    check("a rejected one doesn't count either", c["debug"]["needs_exception"] is False, c["reply"])
    check("Ravi's pending OD-2001 (condoned) is not counted as absence",
          "OD-2001" not in c["reply"] and not c["debug"].get("other_leave_counted"), c["reply"])

    section("stacking: leave already in the past is already in the figures")
    sandbox("json")
    a = leave("U001", "medical", "2026-10-21", "2026-10-22")  # Wed–Thu, before today
    b = leave("U001", "medical", "2026-11-02")
    check("days already gone are not counted a second time",
          not b["debug"].get("other_leave_counted") and b["debug"]["needs_exception"] is False, b["reply"])

    section("the same days twice: re-sent, or overlapping")
    backend = sandbox("json")
    a = leave("U001", "medical", "2026-11-02", "2026-11-04")
    n = len(backend.list_records_for_user("U001")["data"])
    again = leave("U001", "medical", "2026-11-02", "2026-11-04")
    check("an exact repeat is refused, naming the request already on file",
          again["debug"]["outcome"] == "overlaps" and a["debug"]["record_id"] in again["reply"],
          again["reply"])
    check("  and nothing was created", len(backend.list_records_for_user("U001")["data"]) == n)
    part = leave("U001", "od", "2026-11-04", "2026-11-06")
    check("a request that only partly overlaps is refused too",
          part["debug"]["outcome"] == "overlaps", part["reply"])
    edge = leave("U001", "od", "2026-11-05")
    check("the day after the range is fine", edge["debug"]["outcome"] == "created", edge["reply"])
    other = leave("U002", "od", "2026-11-02")
    check("another student's leave on the same days is no clash",
          other["debug"]["outcome"] == "created", other["reply"])
    chat(handlers.handle_cancel_record, "U001", record_id=a["debug"]["record_id"])
    redo = leave("U001", "medical", "2026-11-02", "2026-11-03")
    check("once withdrawn, the days can be applied for again",
          redo["debug"]["outcome"] == "created", redo["reply"])

    section("a staff verb on the wrong kind of record, named by id")
    backend = sandbox("json")
    for uid, verb, rid, other in ((WARDEN, "approve", "CMP-1001", "resolve"),
                                  (WARDEN, "reject", "CMP-1003", "resolve"),
                                  (ADVISOR, "resolve", "OD-2001", "approve")):
        before = backend.get_record(rid)["data"]["status"]
        out = chat(handlers.handle_staff_action, uid, action=verb, record_id=rid)
        check(f"{users.get(uid)['role']} '{verb} {rid}' is a type error that suggests '{other}'",
              out["debug"]["outcome"] == "wrong_action_for_type" and other in out["reply"]
              and backend.get_record(rid)["data"]["status"] == before, out["reply"])
    out = chat(handlers.handle_staff_action, WARDEN, action="approve", record_id="OD-2001")
    check("the warden approving real leave by id is still refused, naming the advisor",
          out["debug"]["outcome"] == "not_authorized" and "Lakshmi" in out["reply"], out["reply"])
    out = chat(handlers.handle_staff_action, WARDEN, action="approve", target_person="Ravi")
    check("with no id, the warden is told about her remit before any lookup",
          out["debug"]["outcome"] == "not_authorized" and "faculty advisor" in out["reply"], out["reply"])

    section("a staff member names a student with more than one request pending")
    backend = sandbox("json")
    med = leave("U001", "medical", "2026-11-02")["debug"]["record_id"]
    out = chat(handlers.handle_staff_action, ADVISOR, action="approve", target_person="Ravi")
    check("asked which, listing both — not the most recent picked",
          out["debug"]["outcome"] == "needs_target" and "OD-2001" in out["reply"] and med in out["reply"],
          out["reply"])
    check("  and neither moved", {backend.get_record(r)["data"]["status"] for r in ("OD-2001", med)}
          == {"submitted"})
    out = chat(handlers.handle_staff_action, ADVISOR, action="approve", target_person="Ravi",
               leave_kind="od")
    check("naming the kind narrows it to the one: OD-2001 approved",
          out["debug"]["outcome"] == "updated" and backend.get_record("OD-2001")["data"]["status"] == "approved"
          and backend.get_record(med)["data"]["status"] == "submitted", out["reply"])
    chat(handlers.handle_file_complaint, "U003", category="lab", description="lab PCs down")
    out = chat(handlers.handle_staff_action, WARDEN, action="resolve", target_person="Arjun")
    check("two open complaints: the warden is asked which one too",
          out["debug"]["outcome"] == "needs_target" and "CMP-1003" in out["reply"], out["reply"])
    out = chat(handlers.handle_staff_action, ADVISOR, action="reject", target_person="Arjun")
    check("one candidate only (OD-2002): acted on directly",
          out["debug"]["outcome"] == "updated"
          and backend.get_record("OD-2002")["data"]["status"] == "rejected", out["reply"])

    section("the whole medical journey, over HTTP")
    backend = sandbox("json")
    ravi, advisor, warden = login("U001"), login(ADVISOR), login(WARDEN)
    rid = leave("U001", "medical", "2026-11-02", "2026-11-04")["debug"]["record_id"]
    r = advisor.post(f"/api/records/{rid}/action", json={"action": "approve"})
    check("no certificate yet: 409 needs_certificate", r.status_code == 409
          and r.json()["outcome"] == "needs_certificate", r.text)
    r = ravi.post(f"/api/records/{rid}/certificate", files={"file": ("c.pdf", PDF, "application/pdf")})
    check("Ravi attaches it", r.status_code == 200, r.text)
    check("the warden can't open it", warden.get(f"/api/records/{rid}/certificate").status_code == 403)
    r = advisor.post(f"/api/records/{rid}/action", json={"action": "approve"})
    check("flagged: 409 needs_exception_ack, with the numbers",
          r.status_code == 409 and r.json()["outcome"] == "needs_exception_ack"
          and "projection" in r.json(), r.text)
    r = advisor.post(f"/api/records/{rid}/action", json={"action": "approve", "grant_exception": True})
    check("with the exception: approved", r.status_code == 200
          and backend.get_record(rid)["data"]["status"] == "approved", r.text)
    out = chat(handlers.handle_cancel_record, "U001", record_id=rid)
    check("Ravi can't withdraw it once approved", out["debug"]["outcome"] == "terminal_state"
          and backend.get_record(rid)["data"]["status"] == "approved", out["reply"])
    verbs = [e["verb"] for e in backend.list_events(rid)["data"]]
    check("the history reads submit -> certificate -> approve",
          verbs[0] == "submit" and verbs[-1] == "approve" and len(verbs) == 3, str(verbs))


# ---------------------------------------------------------------------------
# Part 2: random walks against a reference model
# ---------------------------------------------------------------------------
PPD, THRESHOLD, BACKDATE = 6, 0.75, 7
LIVE = {"submitted", "approved"}
OPEN = {"facility_complaint": "open", "od_request": "submitted"}
APPLIES = {"approve": {"od_request"}, "reject": {"od_request"},
           "resolve": {"facility_complaint"}, "cancel": {"od_request", "facility_complaint"}}
WRITES = {"approve": "approved", "reject": "rejected", "resolve": "resolved", "cancel": "cancelled"}
CAN = {"student": set(), "hostel_warden": {"facility_complaint"}, "faculty_advisor": {"od_request"}}
HTTP = {"updated": 200, "not_found": 404, "not_authorized": 403, "unknown_action": 400,
        "wrong_action_for_type": 400, "terminal_state": 409, "needs_exception_ack": 409,
        "needs_certificate": 409}


class Model:
    """The rules, restated from the README rather than read from handlers."""

    def __init__(self, backend) -> None:
        self.records = {}
        for uid in STUDENTS:
            for r in backend.list_records_for_user(uid)["data"]:
                self.records[r["id"]] = {
                    "type": r["type"], "owner": r["user_id"], "status": r["status"],
                    "kind": r.get("leave_kind"), "start": r.get("start_date"),
                    "end": r.get("end_date"), "flagged": bool(r.get("needs_exception")),
                    "cert": bool(r.get("certificate")),
                }
        self.att = {u: backend.get_attendance(u)["data"] for u in STUDENTS}

    def is_open(self, r) -> bool:
        return r["status"] == OPEN[r["type"]]

    def leave(self, uid, kind, start: date, end: date, today: date):
        """(outcome, flagged) for a new leave request."""
        if kind == "personal":
            return "leave_not_granted", None
        if start < today and (kind == "od" or (today - start).days > BACKDATE):
            return "past_dates", None
        hols = dates.college_holidays(users.get(uid).get("year"))
        if dates.working_days(start, end, hols) == 0:
            return "no_working_days", None
        mine = [r for r in self.records.values()
                if r["owner"] == uid and r["type"] == "od_request" and r["status"] in LIVE]
        if any(r["start"] <= end.isoformat() and start.isoformat() <= r["end"] for r in mine):
            return "overlaps", None

        def ahead(s: date, e: date) -> int:
            return dates.working_days(max(s, today), e, hols)

        other = sum(ahead(date.fromisoformat(r["start"]), date.fromisoformat(r["end"]))
                    for r in mine if r["kind"] != "od")
        new = ahead(start, end) * PPD
        a = self.att[uid]
        projected = (a["attended"] + (new if kind == "od" else 0)) / (a["held"] + other * PPD + new)
        return "created", projected < THRESHOLD

    def staff(self, uid, rid, verb, grant) -> str:
        role = users.get(uid)["role"]
        if not CAN[role]:
            return "not_authorized"
        if verb not in WRITES:
            return "unknown_action"
        r = self.records.get(rid)
        if r is None:
            return "not_found"
        if r["type"] not in APPLIES[verb]:
            return "wrong_action_for_type"
        if r["type"] not in CAN[role]:
            return "not_authorized"
        if not self.is_open(r):
            return "terminal_state"
        if verb == "approve" and r["kind"] == "medical" and not r["cert"]:
            return "needs_certificate"
        if verb == "approve" and r["flagged"] and not grant:
            return "needs_exception_ack"
        return "updated"


def walk(seed: int, steps: int, clients: dict) -> list[str]:
    """One random sequence. Returns what went wrong (empty when nothing did)."""
    rng = random.Random(seed)
    backend = sandbox("json")
    model = Model(backend)
    today = clock.today()
    problems: list[str] = []
    events = {rid: len(backend.list_events(rid)["data"]) for rid in model.records}

    def pick_record():
        ids = sorted(model.records) + ["OD-9999"]
        return rng.choice(ids)

    for step in range(steps):
        op = rng.choice(["complaint", "leave", "leave", "amend", "withdraw", "certificate",
                         "staff_http", "staff_http", "staff_chat", "read"])
        uid = rng.choice(STUDENTS) if op in ("complaint", "leave", "certificate") else \
            rng.choice(STUDENTS + (WARDEN, ADVISOR))
        where = f"seed {seed} step {step}: {op} by {uid}"
        moved = None  # the record this step is expected to write to

        if op == "complaint":
            out = chat(handlers.handle_file_complaint, uid,
                       category=rng.choice(["hostel", "lab", "library", "transport"]),
                       priority=rng.choice([None, "low", "high"]), description="something broke")
            rid = out["debug"].get("record_id")
            if out["debug"]["outcome"] != "created" or not rid:
                problems.append(f"{where}: complaint not created ({out['reply']})")
                continue
            model.records[rid] = {"type": "facility_complaint", "owner": uid, "status": "open",
                                  "kind": None, "start": None, "end": None, "flagged": False,
                                  "cert": False}
            moved = rid

        elif op == "leave":
            kind = rng.choice(["od", "medical", "family_emergency", "personal"])
            start = today + timedelta(days=rng.randint(-9, 25))
            end = start + timedelta(days=rng.choice([0, 0, 1, 2, 4]))
            want, flagged = model.leave(uid, kind, start, end, today)
            out = leave(uid, kind, start.isoformat(), end.isoformat())
            got = out["debug"]["outcome"]
            if got != want:
                problems.append(f"{where}: {kind} {start}..{end} -> {got}, model says {want}")
                continue
            if got == "created":
                rid = out["debug"]["record_id"]
                if bool(out["debug"]["needs_exception"]) != flagged:
                    problems.append(f"{where}: {rid} flagged={out['debug']['needs_exception']}, "
                                    f"model says {flagged} ({out['debug'].get('attendance')})")
                model.records[rid] = {"type": "od_request", "owner": uid, "status": "submitted",
                                      "kind": kind, "start": start.isoformat(), "end": end.isoformat(),
                                      "flagged": bool(out["debug"]["needs_exception"]), "cert": False}
                moved = rid

        elif op in ("amend", "withdraw"):
            rid = pick_record()
            r = model.records.get(rid)
            if op == "amend":
                change = rng.choice([{"priority": "high"}, {"reason": "updated reason"}])
                out = chat(handlers.handle_amend_record, uid, record_id=rid, **change)
            else:
                out = chat(handlers.handle_cancel_record, uid, record_id=rid)
            if r is None:
                want = "not_found"
            elif r["owner"] != uid:
                want = "not_authorized"
            elif not model.is_open(r):
                want = "terminal_state"
            elif op == "withdraw":
                want = "cancelled"
            else:
                fits = "priority" if r["type"] == "facility_complaint" else "reason"
                want = "updated" if fits in change else "not_amendable"
            got = out["debug"]["outcome"]
            if got != want:
                problems.append(f"{where}: {rid} -> {got}, model says {want} ({out['reply']})")
                continue
            if got == "cancelled":
                r["status"] = "cancelled" if r["type"] == "facility_complaint" else "withdrawn"
            if got in ("cancelled", "updated"):
                moved = rid

        elif op == "certificate":
            rid = pick_record()
            r = model.records.get(rid)
            res = clients[uid].post(f"/api/records/{rid}/certificate",
                                    files={"file": ("c.pdf", PDF, "application/pdf")})
            ok = bool(r and r["owner"] == uid and r["kind"] == "medical" and r["status"] == "submitted")
            if (res.status_code == 200) != ok:
                problems.append(f"{where}: certificate on {rid} -> {res.status_code}, model says "
                                f"{'allowed' if ok else 'refused'}")
                continue
            if ok:
                r["cert"] = True
                moved = rid

        elif op in ("staff_http", "staff_chat"):
            rid = pick_record()
            verb = rng.choice(["approve", "approve", "reject", "resolve", "cancel", "escalate"])
            grant = rng.random() < 0.4
            want = model.staff(uid, rid, verb, grant)
            if op == "staff_http":
                res = clients[uid].post(f"/api/records/{rid}/action",
                                        json={"action": verb, "grant_exception": grant})
                got = res.json().get("outcome", "not_authorized" if res.status_code == 403 else "?")
                if res.status_code != HTTP[want] or (want != "updated" and got != want):
                    problems.append(f"{where}: {verb} {rid} -> {res.status_code} {got}, "
                                    f"model says {HTTP[want]} {want}")
                    continue
            else:
                out = chat(handlers.handle_staff_action, uid, action=verb, record_id=rid,
                           grant_exception=grant)
                got = out["debug"]["outcome"]
                # The chat door words "escalate" as an unknown action too; a
                # student is refused before the verb is looked at, in both.
                if got != want:
                    problems.append(f"{where}: chat {verb} {rid} -> {got}, model says {want} "
                                    f"({out['reply']})")
                    continue
            if want == "updated":
                model.records[rid]["status"] = WRITES[verb]
                moved = rid

        else:  # read
            rid = pick_record()
            r = model.records.get(rid)
            res = clients[uid].get(f"/api/records/{rid}")
            staff = users.get(uid)["role"] != "student"
            if r is None:
                ok = res.status_code == 404
            else:
                ok = (res.status_code == 200) == (staff or r["owner"] == uid)
            if not ok:
                problems.append(f"{where}: GET {rid} -> {res.status_code}")
            if staff:
                q = clients[uid].get("/api/queue").json()["records"]
                mine = CAN[users.get(uid)["role"]]
                if any(x["type"] not in mine for x in q):
                    problems.append(f"{where}: queue holds records outside {mine}")

        # The store must now equal the model — every record, every status —
        # and only the record this step meant to write may have new history.
        for rid, r in model.records.items():
            got = backend.get_record(rid)["data"]
            if got is None or got["status"] != r["status"]:
                problems.append(f"{where}: {rid} is {got and got['status']}, model says {r['status']}")
            n = len(backend.list_events(rid)["data"])
            if n != events.get(rid, 0) and rid != moved:
                problems.append(f"{where}: {rid} gained an event on a step that didn't write to it")
            events[rid] = n
        stored = {r["id"] for u in STUDENTS for r in backend.list_records_for_user(u)["data"]}
        if stored != set(model.records):
            problems.append(f"{where}: store holds {sorted(stored ^ set(model.records))} unexpectedly")
        if problems:
            break  # the first divergence is the useful one; later ones are its echo
    return problems


def random_walks() -> None:
    walks = int(os.getenv("WORKFLOW_WALKS", "30"))
    steps = int(os.getenv("WORKFLOW_STEPS", "40"))
    first = int(os.getenv("WORKFLOW_SEED", str(random.randrange(100_000))))
    section(f"random walks: {walks} x {steps} steps, seeds {first}..{first + walks - 1}")
    sandbox("json")
    clients = {uid: login(uid) for uid in PASSWORDS}
    failures = []
    for seed in range(first, first + walks):
        problems = walk(seed, steps, clients)
        if problems:
            failures.append(problems[0])
    check(f"{walks} walks agree with the reference model at every step", not failures,
          "\n       ".join(failures[:5]))


def main() -> int:
    journeys()
    random_walks()
    return finish("workflows")


if __name__ == "__main__":
    raise SystemExit(main())
