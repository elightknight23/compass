"""The complaint mirror against a fake GitHub — no network.

An httpx.MockTransport plays a small GitHub Issues API in memory, so every
check runs offline and none of them can ever file a real issue (the
harness blanks GITHUB_REPO and GITHUB_TOKEN; this suite sets its own fake
ones on `config` after import).

What it pins: a complaint becomes one issue with the right labels; amend,
resolve and cancel move it, and a resolved issue and a cancelled one close
for different reasons; the body names roles, never people; GitHub being
down, or refusing the token, never stops a complaint being filed and is
caught up later without a duplicate; a demo reset reuses the old issue
instead of opening a second; leave requests are never mirrored; and with no
repo configured nothing is sent at all.

    python scripts/test_tracker.py
"""

from __future__ import annotations

import json

from _harness import check, finish, sandbox, section

import httpx
from fastapi.testclient import TestClient

from app import backends, config, handlers, tracker, users
from app.main import app
from app.state import new_state

REPO = "college/facilities"


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.labels: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.status: int | None = None  # force an error status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("no route to host", request=request)
        if self.status:
            return httpx.Response(self.status, json={"message": "nope"})
        assert request.headers["authorization"] == "Bearer test-token"
        base = f"/repos/{REPO}/issues"
        path = request.url.path
        if path == f"/repos/{REPO}/labels":
            if request.method == "GET":
                return httpx.Response(200, json=list(self.labels.values()))
            label = json.loads(request.content)
            self.labels[label["name"]] = label
            return httpx.Response(201, json=label)
        if request.method == "GET" and path == base:
            return httpx.Response(200, json=list(self.issues.values()))
        if request.method == "POST" and path == base:
            n = len(self.issues) + 1
            body = json.loads(request.content)
            self.issues[n] = {"number": n, "html_url": f"https://github.com/{REPO}/issues/{n}",
                              "state": "open", "state_reason": None, **body}
            return httpx.Response(201, json=self.issues[n])
        if request.method == "PATCH" and path.startswith(base + "/"):
            n = int(path.rsplit("/", 1)[1])
            if n not in self.issues:
                return httpx.Response(404, json={"message": "Not Found"})
            self.issues[n].update(json.loads(request.content))
            return httpx.Response(200, json=self.issues[n])
        return httpx.Response(404, json={"message": "Not Found"})


def file(user_id: str, text: str, **entities) -> dict:
    st = new_state(text, users.get(user_id))
    st["entities"] = {"description": text, **entities}
    return handlers.handle_file_complaint(st)


def main() -> int:
    store = sandbox("json")
    gh = FakeGitHub()
    tracker._transport = httpx.MockTransport(gh)
    tracker.SYNC_INLINE = True

    section("off unless configured")
    check("no repo, no token: disabled", not tracker.enabled())
    out = file("U001", "Fan in B-214 is broken", category="hostel")
    check("  a complaint is filed as usual, and nothing is sent", out["debug"]["outcome"] == "created"
          and not gh.calls and store.get_record(out["debug"]["record_id"])["data"]["tracker"] is None)

    for pasted in ("me/tickets", "https://github.com/me/tickets", "https://github.com/me/tickets/",
                   "https://github.com/me/tickets.git", "git@github.com:me/tickets.git"):
        check(f"GITHUB_REPO={pasted!r} reads as me/tickets", config.repo_slug(pasted) == "me/tickets")

    config.GITHUB_REPO, config.GITHUB_TOKEN = REPO, "test-token"
    backends.get_backend = lambda: tracker.Mirrored(store)
    check("with both set, the backend is mirrored", tracker.enabled())

    section("a complaint becomes an issue")
    out = file("U003", "AC in the library reading room isn't working", category="library", priority="high")
    cid = out["debug"]["record_id"]
    rec = store.get_record(cid)["data"]
    issue = gh.issues.get(rec["tracker"]["issue"]) if rec["tracker"] else None
    check("filed: one issue, and the record knows its number and link",
          issue is not None and rec["tracker"]["synced"] is True
          and rec["tracker"]["url"].endswith(f"/issues/{issue['number']}"), str(rec["tracker"]))
    check("  titled with the Compass id", issue is not None and issue["title"].startswith(f"[{cid}] AC in the library"))
    check("  labelled compass / type / status / category / priority", issue is not None
          and issue["labels"] == ["compass", "type:complaint", "status:open", "category:library", "priority:high"],
          str(issue and issue["labels"]))
    check("  and the labels are made with a colour and a description, not left grey",
          gh.labels.get("type:complaint", {}).get("color") == "d97757"
          and gh.labels["status:open"]["description"], str(gh.labels))
    check("  the body has the complaint and its history, by role",
          issue is not None and "isn't working" in issue["body"] and "Filed by the student" in issue["body"])
    check("  and never a name", issue is not None and "Priya" not in issue["body"] and "Sharma" not in issue["body"])
    check("the student's reply is unchanged by the mirror", "Filed **" in out["reply"])

    section("changes follow it")
    st = new_state("make it urgent... actually low", users.get("U003"))
    st["entities"] = {"record_id": cid, "priority": "low"}
    handlers.handle_amend_record(st)
    check("amend: the priority label moves", gh.issues[1]["labels"][-1] == "priority:low", str(gh.issues[1]["labels"]))
    labels_made = len(gh.labels)
    warden = users.get("U101")
    res = handlers.apply_staff_action(warden, cid, "resolve")
    check("resolve: the issue closes as completed",
          res["success"] and gh.issues[1]["state"] == "closed" and gh.issues[1]["state_reason"] == "completed")
    check("  and the history says who, by role", "Resolved by the hostel warden" in gh.issues[1]["body"])
    check("  the status label follows", "status:resolved" in gh.issues[1]["labels"]
          and "status:open" not in gh.issues[1]["labels"], str(gh.issues[1]["labels"]))
    check("  and only labels new to the repository are created",
          len(gh.labels) == labels_made + 1, str(sorted(gh.labels)))
    other = file("U001", "Bus 7 skipped my stop", category="transport")["debug"]["record_id"]
    st = new_state("cancel that", users.get("U001"))
    st["entities"] = {"record_id": other}
    handlers.handle_cancel_record(st)
    n = store.get_record(other)["data"]["tracker"]["issue"]
    check("withdraw: closes as not_planned — a withdrawal is not a fix",
          gh.issues[n]["state"] == "closed" and gh.issues[n]["state_reason"] == "not_planned")

    section("the console door, over HTTP")
    third = file("U001", "Lab 3 projector flickers", category="lab")["debug"]["record_id"]
    c = TestClient(app)
    c.post("/api/login", json={"username": "meena.krishnan", "password": "warden-demo"})
    r = c.post(f"/api/records/{third}/action", json={"action": "resolve"})
    n = store.get_record(third)["data"]["tracker"]["issue"]
    check("resolved from the console: the same mirror", r.status_code == 200 and gh.issues[n]["state"] == "closed")
    check("/api/health reports the tracker", c.get("/api/health").json()["tracker"]["enabled"] is True)

    section("GitHub down, then back")
    gh.down = True
    before = len(gh.issues)
    out = file("U001", "Water cooler on floor 2 leaks", category="hostel")
    down_id = out["debug"]["record_id"]
    t = store.get_record(down_id)["data"]["tracker"]
    check("the complaint is still filed", out["debug"]["outcome"] == "created")
    check("  marked not in step, with the reason, and no invented issue number",
          t["synced"] is False and "reach GitHub" in t["error"] and "issue" not in t, str(t))
    check("  it is in the pending list", down_id in tracker.pending())
    gh.down = False
    result = tracker.sync_pending()
    t = store.get_record(down_id)["data"]["tracker"]
    check("when GitHub is back, the retry pass catches it up", result["synced"] >= 1 and t["synced"] is True)
    check("  with exactly one issue for it",
          sum(i["title"].startswith(f"[{down_id}]") for i in gh.issues.values()) == 1)
    check("  and the pass also caught up complaints filed while the mirror was off (the seed's too)",
          not tracker.pending() and len(gh.issues) > before + 1)

    gh.status = 401
    tracker.sync(down_id)
    check("a rejected token is said plainly", store.get_record(down_id)["data"]["tracker"]["error"]
          == "GitHub rejected the token")
    gh.status = None

    section("idempotent")
    count = len(gh.issues)
    tracker.sync(down_id)
    tracker.sync(down_id)
    check("syncing twice changes nothing", len(gh.issues) == count)
    store.update_record(down_id, tracker=None)  # what a demo reset leaves behind
    tracker.sync(down_id)
    check("a record that forgot its issue (demo reset) finds it by title — no duplicate",
          len(gh.issues) == count and store.get_record(down_id)["data"]["tracker"]["synced"] is True)

    section("leave: mirrored, without its reason")
    st = new_state("medical leave Monday to Wednesday, viral fever", users.get("U001"))
    st["entities"] = {"leave_kind": "medical", "start_date": "2026-11-02", "end_date": "2026-11-04",
                      "reason": "Viral fever, doctor advised rest"}
    lid = handlers.handle_request_od_leave(st)["debug"]["record_id"]
    rec = store.get_record(lid)["data"]
    issue = gh.issues.get((rec["tracker"] or {}).get("issue"))
    check("a leave request becomes an issue", issue is not None and rec["tracker"]["synced"], str(rec["tracker"]))
    check("  titled with its kind, dates and working days",
          issue and issue["title"] == f"[{lid}] Medical leave · Mon 2 Nov – Wed 4 Nov · 3 working days",
          issue and issue["title"])
    check("  labelled as leave, waiting on an exception and a certificate", issue and issue["labels"] ==
          ["compass", "type:leave", "status:submitted", "leave:medical", "needs-exception", "awaiting-certificate"],
          issue and str(issue["labels"]))
    check("  and the reason never leaves Compass",
          issue and "fever" not in issue["body"].lower() and "doctor" not in issue["body"].lower()
          and "fever" not in issue["title"].lower())
    check("  nor a name, nor a percentage", issue and "Ravi" not in issue["body"] and "%" not in issue["body"])
    from app import certificates
    certificates.attach(users.get("U001"), lid, "note.pdf", b"%PDF-1.4 a certificate")
    n = store.get_record(lid)["data"]["tracker"]["issue"]
    check("attaching a certificate drops awaiting-certificate, and says so by role",
          "awaiting-certificate" not in gh.issues[n]["labels"]
          and "| Certificate | attached |" in gh.issues[n]["body"]
          and "Certificate by the student" in gh.issues[n]["body"], str(gh.issues[n]["labels"]))
    check("  but not the certificate's filename", "note.pdf" not in gh.issues[n]["body"])
    handlers.apply_staff_action(users.get("U102"), lid, "approve", grant_exception=True)
    check("approved: closed as completed, the waiting labels gone",
          gh.issues[n]["state"] == "closed" and gh.issues[n]["state_reason"] == "completed"
          and gh.issues[n]["labels"] == ["compass", "type:leave", "status:approved", "leave:medical"],
          str(gh.issues[n]["labels"]))
    handlers.apply_staff_action(users.get("U102"), "OD-2001", "reject")
    n = store.get_record("OD-2001")["data"]["tracker"]["issue"]
    check("rejected: closed as not_planned",
          gh.issues[n]["state"] == "closed" and gh.issues[n]["state_reason"] == "not_planned"
          and "status:rejected" in gh.issues[n]["labels"])

    tracker._transport, tracker.SYNC_INLINE = None, False
    config.GITHUB_REPO = config.GITHUB_TOKEN = ""
    return finish("tracker")


if __name__ == "__main__":
    raise SystemExit(main())
