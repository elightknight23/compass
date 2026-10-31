"""Medical certificates over the real HTTP stack — no model.

Who may attach one (the student, for their own undecided medical leave),
who may open one (the student and whoever decides the leave — not the
warden, who is staff and reads every record), what the warden's copy of
the record says instead, and the approval gate: medical leave cannot be
approved until a certificate is attached.

    python scripts/test_certificates.py
"""

from __future__ import annotations

from _harness import check, finish, sandbox, section

from fastapi.testclient import TestClient

from app import config, handlers, users
from app.main import app
from app.state import new_state

PASSWORDS = {
    "ravi.kumar": "ravi-demo",
    "arjun.menon": "arjun-demo",
    "meena.krishnan": "warden-demo",
    "lakshmi.narayanan": "advisor-demo",
}
PDF = b"%PDF-1.4\n% a certificate\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def client_for(username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/login", json={"username": username, "password": PASSWORDS[username]})
    assert r.status_code == 200, f"login failed for {username}: {r.text}"
    return c


def attach(c: TestClient, record_id: str, name: str = "cert.pdf", data: bytes = PDF):
    return c.post(f"/api/records/{record_id}/certificate", files={"file": (name, data)})


def main() -> int:
    backend = sandbox("json")
    ravi, arjun = client_for("ravi.kumar"), client_for("arjun.menon")
    warden, advisor = client_for("meena.krishnan"), client_for("lakshmi.narayanan")

    st = new_state("I'm down with fever, medical leave Monday to Wednesday", users.get("U001"))
    st["entities"] = {"leave_kind": "medical", "start_date": "2026-11-02", "end_date": "2026-11-04"}
    medical = handlers.handle_request_od_leave(st)["debug"]["record_id"]

    section("the approval gate")
    r = advisor.post(f"/api/records/{medical}/action", json={"action": "approve", "grant_exception": True})
    check("medical leave with no certificate can't be approved: 409 needs_certificate",
          r.status_code == 409 and r.json()["outcome"] == "needs_certificate", r.text)
    check("  and the record did not move", backend.get_record(medical)["data"]["status"] == "submitted")

    section("attaching: the student, for their own medical leave")
    r = attach(arjun, medical)
    check("another student can't attach one to Ravi's leave (403)", r.status_code == 403, r.text)
    r = attach(advisor, medical)
    check("  nor can the advisor, on his behalf (403)", r.status_code == 403, r.text)
    check("  and nothing was stored", backend.get_record(medical)["data"]["certificate"] is None
          and not any(config.CERTIFICATES_DIR.glob(f"{medical}*")))
    check("a .docx is refused (415)", attach(ravi, medical, "cert.docx", b"PK\x03\x04").status_code == 415)
    check("a .pdf that isn't a PDF is refused (415)", attach(ravi, medical, "cert.pdf", b"hello").status_code == 415)
    check("an empty file is refused (400)", attach(ravi, medical, "cert.pdf", b"").status_code == 400)
    saved = config.MAX_CERTIFICATE_MB
    config.MAX_CERTIFICATE_MB = 0
    check("an oversized file is refused (413)", attach(ravi, medical).status_code == 413)
    config.MAX_CERTIFICATE_MB = saved
    check("an unknown record is 404", attach(ravi, "OD-9999").status_code == 404)
    check("OD isn't medical leave, so it takes no certificate (400)",
          attach(ravi, "OD-2001").status_code == 400)

    r = attach(ravi, medical, "ravi_dengue_report.png", PNG)
    check("Ravi attaches a photo: 200, metadata on the record", r.status_code == 200
          and r.json()["record"]["certificate"]["content_type"] == "image/png", r.text)
    check("  the file is stored under the record's id", (config.CERTIFICATES_DIR / f"{medical}.png").exists())
    r = attach(ravi, medical, "certificate.pdf", PDF)
    check("replacing it with a PDF removes the photo",
          r.status_code == 200 and (config.CERTIFICATES_DIR / f"{medical}.pdf").exists()
          and not (config.CERTIFICATES_DIR / f"{medical}.png").exists())
    verbs = [(e["verb"], e["note"]) for e in backend.list_events(medical)["data"]]
    check("  both are in the record's history",
          ("certificate", "certificate attached") in verbs and ("certificate", "certificate replaced") in verbs,
          str(verbs))

    section("reading: the student and the advisor — not the warden")
    check("Ravi can open his own", ravi.get(f"/api/records/{medical}/certificate").content == PDF)
    r = advisor.get(f"/api/records/{medical}/certificate")
    check("the advisor can open it", r.status_code == 200 and r.content == PDF)
    check("  and it is not cached", "no-store" in r.headers.get("cache-control", ""))
    check("the warden, who reads every record, can't open it (403)",
          warden.get(f"/api/records/{medical}/certificate").status_code == 403)
    check("another student can't (403)", arjun.get(f"/api/records/{medical}/certificate").status_code == 403)
    check("nobody signed in can't (401)",
          TestClient(app).get(f"/api/records/{medical}/certificate").status_code == 401)
    seen = warden.get(f"/api/records/{medical}").json()["record"]["certificate"]
    check("the warden's copy of the record says only that one is attached — no filename",
          seen == {"attached": True}, str(seen))
    check("the advisor's queue copy has the details",
          next(x for x in advisor.get("/api/queue").json()["records"] if x["id"] == medical)
          ["certificate"]["filename"] == "certificate.pdf")

    section("then it can be decided")
    r = advisor.post(f"/api/records/{medical}/action", json={"action": "approve", "grant_exception": True})
    check("with the certificate attached, the advisor can approve it", r.status_code == 200, r.text)
    check("a decided request's certificate can't be swapped (409)", attach(ravi, medical).status_code == 409)
    check("  but stays readable", ravi.get(f"/api/records/{medical}/certificate").status_code == 200)
    reject_me = handlers.handle_request_od_leave(st)["debug"]["record_id"]
    r = advisor.post(f"/api/records/{reject_me}/action", json={"action": "reject"})
    check("rejecting medical leave doesn't need a certificate", r.status_code == 200, r.text)

    return finish("certificates")


if __name__ == "__main__":
    raise SystemExit(main())
