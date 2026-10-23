"""Authentication and authorization over the real HTTP stack — no LLM.

Identity comes from a signed session cookie and nowhere else. These checks
make that claim testable: a forged header is ignored, a tampered cookie is
anonymous, and every staff action is re-checked below the HTTP layer.

The headline check is the one the design insists on keeping: a refused
action POSTed straight at the endpoint returns 403 **and the record did not
move**. An endpoint can refuse loudly and still have written the row; only
the second assertion catches that.

The chat endpoint is exercised too, including its event stream. With the
model keys blanked by the harness, the classifier fails over to `clarify`
and the clarify handler to its fixed sentence — so even the stream test
makes no network call, and it doubles as a test of that designed fallback.

    python scripts/test_auth.py
"""

from __future__ import annotations

import json

from _harness import check, finish, sandbox, section

from fastapi.testclient import TestClient

from app import auth, handlers
from app.main import app

PASSWORDS = {
    "ravi.kumar": "ravi-demo",
    "arjun.menon": "arjun-demo",
    "meena.krishnan": "warden-demo",
    "lakshmi.narayanan": "advisor-demo",
}


def client_for(username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/login", json={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 200, f"login failed for {username}: {r.text}"
    return c


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines.get("data", "{}"))))
    return events


def main() -> int:
    backend = sandbox("json")

    section("password storage")
    encoded = auth.hash_password("correct horse")
    check("a hash is scrypt, salted, and not the password",
          encoded.startswith("scrypt$") and "correct horse" not in encoded)
    check("the right password verifies", auth.verify_password("correct horse", encoded))
    check("the wrong password does not", not auth.verify_password("wrong", encoded))
    check("two hashes of one password differ (per-user salt)",
          auth.hash_password("same") != auth.hash_password("same"))
    check("a malformed hash fails closed rather than raising",
          not auth.verify_password("x", "not-a-real-hash"))

    section("credentials, through the backend")
    check("a seeded account authenticates",
          (auth.authenticate("lakshmi.narayanan", "advisor-demo") or {}).get("id") == "U102")
    check("usernames are case-insensitive",
          (auth.authenticate("Ravi.Kumar", "ravi-demo") or {}).get("id") == "U001")
    check("a wrong password is rejected", auth.authenticate("ravi.kumar", "nope") is None)
    check("an unknown username is rejected", auth.authenticate("mallory", "x") is None)

    section("sessions")
    token = auth.issue_session("U001")
    check("a fresh token resolves to its user", (auth.read_session(token) or {}).get("id") == "U001")
    payload, sig = token.rsplit(".", 1)
    forged = f"{payload}.{'A' * len(sig)}"
    check("a forged signature is rejected", auth.read_session(forged) is None)
    swapped = token.rsplit(".", 1)[0] + "." + auth.issue_session("U102").rsplit(".", 1)[1]
    check("a signature from another token is rejected", auth.read_session(swapped) is None)
    check("an expired token is rejected", auth.read_session(auth.issue_session("U001", ttl=-1)) is None)
    check("garbage is rejected", auth.read_session("hello") is None)
    check("an empty token is rejected", auth.read_session(None) is None)
    check("a token for an unknown user resolves to nobody",
          auth.read_session(auth.issue_session("U999")) is None)

    section("identity comes from the cookie, never a header or a message")
    anon = TestClient(app)
    check("chat without a session is refused",
          anon.post("/api/chat", json={"message": "hi"}).status_code == 401)
    r = anon.post("/api/chat", json={"message": "Approve Ravi's OD"},
                  headers={"X-User-Id": "U102"})
    check("a forged X-User-Id header is ignored", r.status_code == 401, f"{r.status_code}")
    check("  and it does not open the staff queue either",
          anon.get("/api/queue", headers={"X-User-Id": "U102"}).status_code == 401)
    tampered = TestClient(app)
    tampered.cookies.set(auth.SESSION_COOKIE, forged)
    check("a tampered cookie is anonymous", tampered.get("/api/me").status_code == 401)

    section("login flow")
    c = TestClient(app)
    r = c.post("/api/login", json={"username": "ravi.kumar", "password": "wrong"})
    check("bad credentials are refused", r.status_code == 401)
    check("  and the message does not say which half was wrong",
          "username or password" in r.json()["detail"].lower())
    r = c.post("/api/login", json={"username": "ravi.kumar", "password": "ravi-demo"})
    check("a good login succeeds", r.status_code == 200, r.text)
    check("  and sets an HttpOnly session cookie",
          "httponly" in r.headers.get("set-cookie", "").lower())
    check("  and never returns a password or hash", "password" not in r.text and "scrypt" not in r.text)
    me = c.get("/api/me").json()["user"]
    check("/api/me names the user and their (empty) capabilities",
          me["id"] == "U001" and me["capabilities"] == [], str(me))
    c.post("/api/logout")
    check("logout ends the session", c.get("/api/me").status_code == 401)

    section("students")
    ravi = client_for("ravi.kumar")
    check("a student may not read the staff queue", ravi.get("/api/queue").status_code == 403)
    check("a student may not enumerate the directory", ravi.get("/api/users").status_code == 403)
    check("a student may not reindex", ravi.post("/api/admin/reindex").status_code == 403)
    r = ravi.post("/api/records/OD-2001/action", json={"action": "approve"})
    check("a student may not approve their own request over HTTP",
          r.status_code == 403 and backend.get_record("OD-2001")["data"]["status"] == "submitted",
          f"{r.status_code} {r.text}")
    mine = ravi.get("/api/records").json()["records"]
    check("/api/records returns only the caller's own",
          mine and all(x["user_id"] == "U001" for x in mine), str([x["id"] for x in mine]))
    check("a student may read their own record", ravi.get("/api/records/OD-2001").status_code == 200)
    check("a student may NOT read someone else's", ravi.get("/api/records/OD-2002").status_code == 403)
    check("a missing record is 404, not 403", ravi.get("/api/records/OD-9999").status_code == 404)
    att = ravi.get("/api/attendance/me").json()
    check("/api/attendance/me is computed by attendance.py",
          att["attended"] == 457 and round(att["current_pct"], 4) == round(457 / 600, 4)
          and att["days_of_margin"] == 1, str(att))

    section("staff: the queue is scoped to what you can act on")
    warden = client_for("meena.krishnan")
    advisor = client_for("lakshmi.narayanan")
    q = warden.get("/api/queue").json()
    check("the warden's queue holds complaints and nothing else",
          q["records"] and {x["type"] for x in q["records"]} == {"facility_complaint"}
          and q["scope"] == ["facility_complaint"], str(q["scope"]))
    q = advisor.get("/api/queue").json()
    check("the advisor's queue holds leave requests and nothing else",
          q["records"] and {x["type"] for x in q["records"]} == {"od_request"})
    check("  and carries a name directory without credentials",
          "U001" in q["users"] and "password" not in json.dumps(q["users"]))
    check("staff may still READ any record (the first question)",
          warden.get("/api/records/OD-2002").status_code == 200)

    section("the console writes — and refuses without writing")
    r = warden.post("/api/records/OD-2001/action", json={"action": "approve"})
    check("the warden may NOT approve leave (staff vs staff): 403",
          r.status_code == 403, f"{r.status_code} {r.text}")
    check("  the refusal names the faculty advisor", "faculty advisor" in r.text)
    check("  and the record genuinely did not move",
          backend.get_record("OD-2001")["data"]["status"] == "submitted")
    check("  and no audit event was written", backend.list_events("OD-2001")["data"] == [])

    r = advisor.post("/api/records/CMP-1001/action", json={"action": "resolve"})
    check("the advisor may NOT resolve a complaint — the split runs both ways",
          r.status_code == 403 and backend.get_record("CMP-1001")["data"]["status"] == "open")

    r = advisor.post("/api/records/OD-2001/action", json={"action": "approve"})
    check("the advisor approves from the console",
          r.status_code == 200 and r.json()["record"]["status"] == "approved", r.text)
    check("  and the backend really changed",
          backend.get_record("OD-2001")["data"]["status"] == "approved")
    r = advisor.get("/api/records/OD-2001").json()
    check("  and the record's history shows who decided",
          [(e["verb"], e["actor_id"]) for e in r["events"]] == [("approve", "U102")], str(r["events"]))

    r = advisor.post("/api/records/OD-2001/action", json={"action": "reject"})
    check("acting on a decided record is 409, distinct from 403 and 404",
          r.status_code == 409 and r.json()["outcome"] == "terminal_state", r.text)

    r = advisor.post("/api/records/OD-2002/action", json={"action": "approve"})
    check("approving a flagged request without the exception is 409",
          r.status_code == 409 and r.json()["outcome"] == "needs_exception_ack", r.text)
    check("  and returns the projection for the console to show",
          r.json().get("projection", {}).get("projected_pct", 1) < 0.75)
    check("  and did not write", backend.get_record("OD-2002")["data"]["status"] == "submitted")
    r = advisor.post("/api/records/OD-2002/action",
                     json={"action": "approve", "grant_exception": True})
    check("with grant_exception it is approved",
          r.status_code == 200 and r.json()["exception_granted"] is True, r.text)

    r = warden.post("/api/records/CMP-1001/action", json={"action": "approve"})
    check("approving a complaint is 400 — a bad request, not a dead upstream",
          r.status_code == 400 and r.json()["outcome"] == "wrong_action_for_type", r.text)
    r = warden.post("/api/records/CMP-9999/action", json={"action": "resolve"})
    check("a missing record is 404", r.status_code == 404)
    r = warden.post("/api/records/CMP-1001/action", json={"action": "teleport"})
    check("an unknown action is 400", r.status_code == 400)

    section("the rule lives below the HTTP layer")
    res = handlers.apply_staff_action(
        {"id": "U001", "name": "Ravi Kumar", "role": "student"}, "CMP-1003", "resolve"
    )
    check("calling the action core directly as a student still refuses",
          res["outcome"] == "not_authorized" and res["authz"] == "denied")
    check("  so the HTTP dependency is a convenience, not the guarantee",
          backend.get_record("CMP-1003")["data"]["status"] == "open")

    section("chat: JSON and the event stream (offline — designed fallback)")
    r = ravi.post("/api/chat", json={"message": "hello", "history": []})
    body = r.json()
    check("plain JSON chat returns a reply and a trace",
          r.status_code == 200 and body["reply"] and "intent" in body["debug"], r.text[:200])
    check("  with no model reachable, it asks rather than guessing (clarify)",
          body["debug"].get("intent") == "clarify" and "classifier_error" in body["debug"])
    r = ravi.post("/api/chat", json={"message": "hello"},
                  headers={"Accept": "text/event-stream"})
    events = parse_sse(r.text)
    check("the stream is text/event-stream",
          r.headers.get("content-type", "").startswith("text/event-stream"))
    check("  and emits the classification BEFORE the reply",
          [e for e, _ in events] == ["classification", "reply"], str([e for e, _ in events]))
    check("  the reply event carries the full trace",
          events and "latency_ms" in events[-1][1]["debug"] and events[-1][1]["reply"])

    return finish("auth / HTTP")


if __name__ == "__main__":
    raise SystemExit(main())
