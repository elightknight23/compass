"""Compass — the College's campus service desk.

Students ask one chat window about the regulations, file hostel, lab,
library and transport complaints, and apply for OD or leave. Wardens resolve
complaints; faculty advisors decide leave.

This module stays deliberately thin. It resolves who is asking, builds the
state, runs the graph, and returns the reply plus the reasoning trace. All
decisions live in app/graph.py and app/handlers.py.

**Identity comes from a signed session cookie and nowhere else.** A message
that says "I'm the warden" changes nothing; neither does a forged header.

**Two doors, one lock.** Chat reaches a staff mutation through the graph;
the staff console reaches it through POST /api/records/{id}/action. Both
land in `handlers.apply_staff_action`, which re-checks the role itself. The
HTTP dependency refusing early is a convenience, not the guarantee — hiding
a button is not an authorization mechanism.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import (
    BackgroundTasks, Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    attendance, auth, backends, certificates, clock, config, handlers, library, rag, tracker, users,
    warmup,
)
from .graph import HISTORY_TURNS, graph
from .state import new_state

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Cold start (model connection, ONNX weights, vector segments) is paid on
    # a daemon thread, so the port binds immediately and the cost lands
    # while the demo is still being set up. See app/warmup.py.
    # An upload interrupted by the last shutdown would otherwise sit in
    # "processing" forever, undeletable.
    library.recover_interrupted()
    warmup.warm_in_background()
    # Complaints not yet mirrored to the tracker (it was down, or this is
    # its first run) catch up now, then on a timer.
    stop = tracker.start_retry_loop()
    yield
    stop.set()


app = FastAPI(title="Compass", lifespan=lifespan)

# Fields safe to hand to a browser. Credentials live in their own store
# precisely so this can stay a pass-through — but listing fields explicitly
# means a future one is opt-in, not opt-out.
PUBLIC_USER_FIELDS = (
    "id", "username", "name", "role", "department", "branch", "year", "semester",
    "section", "residence",
)


def public_user(user: dict) -> dict:
    # `capabilities` is derived, not stored: the browser needs to know which
    # buttons to draw, and shipping the rule's *result* rather than a copy
    # of the rule keeps the UI from drifting away from auth.py. Every action
    # is still re-checked server-side.
    return {
        **{k: user.get(k) for k in PUBLIC_USER_FIELDS},
        "capabilities": sorted(auth.capabilities(user)),
    }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def current_user(compass_session: str | None = Cookie(default=None)) -> dict | None:
    """The actor, or None. Every failure mode of a token — tampered, expired,
    malformed, unknown user — arrives here as None. There is no partial trust."""
    return auth.read_session(compass_session)


def require_user(user: dict | None = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return user


def require_staff(user: dict = Depends(require_user)) -> dict:
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="This is limited to staff accounts.")
    return user


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Turn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    # The browser holds the conversation log; it replays the tail so the
    # classifier can resolve follow-ups ("Which one?" -> "OD-2001"). A
    # sliding window, not a server-side checkpointer.
    history: list[Turn] = Field(default_factory=list)


class LoginRequest(BaseModel):
    username: str
    password: str


class ActionRequest(BaseModel):
    action: str
    # Approving a request flagged for an attendance shortage needs this set,
    # deliberately — the console asks the advisor before it sends it.
    grant_exception: bool = False


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/login")
def login(req: LoginRequest, response: Response):
    user = auth.authenticate(req.username, req.password)
    if not user:
        # One message for both failure modes. Saying "no such user" would
        # turn the login form into an account-enumeration oracle.
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=auth.issue_session(user["id"]),
        httponly=True,  # page JavaScript cannot read it
        samesite="lax",
        max_age=None,  # session cookie; the token carries its own expiry
        path="/",
    )
    print(f"[compass] login user={user['id']} ({user['username']})")
    return {"user": public_user(user)}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(require_user)):
    return {"user": public_user(user)}


@app.get("/api/users")
def list_users(user: dict = Depends(require_staff)):
    """The people directory. Staff-only: a student has no business
    enumerating classmates."""
    return [public_user(u) for u in users.all_users()]


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


def _classification_event(debug: dict) -> dict:
    """What the classifier heard and what Python resolved, before the answer.

    Emitted first so the UI can show the seam — `heard` beside `resolved` —
    while the handler is still working. That seam being visible is the
    point of the design, not debug output.
    """
    return {
        k: debug.get(k)
        for k in ("intent", "confidence", "reasoning", "entities", "dates",
                  "provider", "downgraded_from", "classifier_error")
        if debug.get(k) is not None
    }


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, user: dict = Depends(require_user)):
    """One turn, run as the signed-in user.

    With `Accept: text/event-stream` this streams two events — `classification`
    as soon as the classifier returns, then `reply` with the answer and the
    full trace. Without it, it returns the same final payload as plain JSON,
    which is what scripts and curl want.
    """
    started = time.perf_counter()
    state = new_state(
        message=req.message,
        user=user,
        history=[t.model_dump() for t in req.history[-HISTORY_TURNS:]],
    )

    def log(debug: dict) -> None:
        print(
            f"[compass] user={user['id']} intent={debug.get('intent')} "
            f"conf={debug.get('confidence')} provider={debug.get('provider')} "
            f"outcome={debug.get('outcome')} {debug.get('latency_ms')}ms"
        )

    if "text/event-stream" not in request.headers.get("accept", ""):
        final = graph().invoke(state)
        debug = dict(final.get("debug") or {})
        debug["latency_ms"] = round((time.perf_counter() - started) * 1000)
        log(debug)
        return {"reply": final["reply"], "debug": debug}

    def stream():
        debug: dict = dict(state["debug"])
        reply = ""
        try:
            for update in graph().stream(state, stream_mode="updates"):
                for node, delta in update.items():
                    debug |= (delta or {}).get("debug") or {}
                    if node == "classify":
                        yield _sse("classification", _classification_event(debug))
                    if (delta or {}).get("reply"):
                        reply = delta["reply"]
        except Exception as e:  # noqa: BLE001 — a broken stream is worse than a sentence
            debug["error"] = f"{type(e).__name__}: {e}"
            reply = (
                "Something went wrong on my side and I don't have an answer for "
                "you. Nothing was changed."
            )
        debug["latency_ms"] = round((time.perf_counter() - started) * 1000)
        log(debug)
        yield _sse("reply", {"reply": reply, "debug": debug})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def _unwrap(res: dict, what: str):
    if not res["success"]:
        raise HTTPException(status_code=502, detail=f"Couldn't reach {what}: {res['error']}")
    return res["data"]


@app.get("/api/records")
def my_records(user: dict = Depends(require_user)):
    """Your own records, newest first. Everybody's own, whatever their role."""
    rows = _unwrap(backends.get_backend().list_records_for_user(user["id"]), "records")
    rows.sort(key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)
    return {"records": rows, "backend": backends.get_backend().name}


@app.get("/api/records/{record_id}")
def get_record(record_id: str, user: dict = Depends(require_user)):
    """One record and its history: your own always, anyone's if you are staff.

    That is the *first* authorization question — reading. Whether you may
    change it is the second, and it is asked only where the change happens.
    """
    backend = backends.get_backend()
    record = _unwrap(backend.get_record(record_id), "records")
    if record is None:
        raise HTTPException(status_code=404, detail=f"No record {record_id} exists.")
    if record["user_id"] != user["id"] and not auth.is_staff(user):
        raise HTTPException(status_code=403, detail=f"{record['id']} isn't yours.")
    events = backend.list_events(record["id"])
    owner = users.get(record["user_id"])
    return {
        "record": certificates.public(record, user),
        "owner": public_user(owner) if owner else None,
        "events": events["data"] if events["success"] else [],
    }


@app.get("/api/attendance/me")
def my_attendance(user: dict = Depends(require_user)):
    """Current percentage and the inputs it came from. Computed here, by
    attendance.py — the same function the leave projection uses."""
    row = _unwrap(backends.get_backend().get_attendance(user["id"]), "attendance")
    if row is None:
        raise HTTPException(status_code=404, detail="No attendance on file for this account.")
    proj = attendance.project(row["attended"], row["held"], 0, False)
    return {
        **row,
        "current_pct": proj.current_pct,
        "threshold": proj.threshold,
        "periods_per_day": proj.periods_per_day,
        "below_threshold": proj.already_below,
        "days_of_margin": attendance.days_of_margin(row["attended"], row["held"]),
    }


CERTIFICATE_OUTCOME_STATUS = {
    "not_signed_in": 401,
    "not_authorized": 403,
    "not_found": 404,
    "invalid": 400,
    "empty": 400,
    "unsupported_type": 415,
    "too_large": 413,
    "terminal_state": 409,
    "unavailable": 502,
}


@app.post("/api/records/{record_id}/certificate")
async def attach_certificate(record_id: str, file: UploadFile = File(...),
                             user: dict = Depends(require_user)):
    """The student attaches (or replaces) the certificate for their own
    medical leave. certificates.attach checks who is asking, itself."""
    data = await file.read(config.MAX_CERTIFICATE_MB * 1024 * 1024 + 1)
    res = certificates.attach(user, record_id, file.filename or "certificate", data)
    if not res["success"]:
        return JSONResponse(status_code=CERTIFICATE_OUTCOME_STATUS.get(res["outcome"], 400),
                            content={"detail": res["error"], "outcome": res["outcome"]})
    return {"outcome": res["outcome"], "record": res["data"]}


@app.get("/api/records/{record_id}/certificate")
def certificate_file(record_id: str, user: dict = Depends(require_user)):
    """The student's own, or one the caller may decide on — not every staff
    member's: a warden reads records, not medical certificates."""
    res = certificates.open_file(user, record_id)
    if not res["success"]:
        raise HTTPException(status_code=CERTIFICATE_OUTCOME_STATUS.get(res["outcome"], 400),
                            detail=res["error"])
    f = res["data"]
    return FileResponse(f["path"], media_type=f["content_type"], filename=f["filename"],
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# Staff console
# ---------------------------------------------------------------------------
@app.get("/api/queue")
def queue(user: dict = Depends(require_staff)):
    """The work this staff member can actually do, and nothing else.

    Scoped to `auth.capabilities`, the same partition that decides which
    buttons appear: the warden gets complaints, the advisor gets leave. A
    record nobody at this desk can act on is not "read-only context", it is
    someone else's inbox — it inflates the "needs action" count and makes
    the queue a worse signal for both roles.

    Filtering here rather than in the browser matters: a client-side filter
    still ships every record to a machine that has no business holding
    them, and leaves the decision to whichever script asks the endpoint.
    A store failure is reported rather than hidden — an empty list would
    claim there is no work waiting, which is a different and worse thing to
    tell someone than "I couldn't look".
    """
    caps = tuple(sorted(auth.capabilities(user)))
    backend = backends.get_backend()
    res = backend.list_all_records(caps)
    return {
        "records": [certificates.public(r, user) for r in res["data"] or []] if res["success"] else [],
        "users": {u["id"]: public_user(u) for u in users.all_users()},
        "backend": backend.name,
        # Which record types this payload speaks for. Without it the browser
        # cannot tell "no complaints are open" from "complaints aren't yours".
        "scope": list(caps),
        "errors": [] if res["success"] else [res["error"]],
    }


# How each refusal reads over HTTP. "You may not" (403), "no such record"
# (404) and "that record is already decided" (409) are three different
# answers, here as in chat.
OUTCOME_STATUS = {
    "not_found": 404,
    "not_authorized": 403,
    "unknown_action": 400,
    "wrong_action_for_type": 400,  # approving a complaint is a bad request
    "terminal_state": 409,
    "needs_exception_ack": 409,
    "needs_certificate": 409,
}


@app.post("/api/records/{record_id}/action")
def record_action(record_id: str, req: ActionRequest, user: dict = Depends(require_staff)):
    """Approve / reject / resolve / cancel one record from the console.

    Delegates to the same function the chat path uses, which re-checks the
    role rather than trusting the dependency above. Same rule, same audit
    trail, same envelope — the console is a different door, not a shortcut.
    """
    res = handlers.apply_staff_action(
        user, record_id, req.action, grant_exception=req.grant_exception
    )

    if not res["success"]:
        status = OUTCOME_STATUS.get(res.get("outcome"), 502)
        print(
            f"[compass] console user={user['id']} {req.action} {record_id} "
            f"-> {res.get('outcome')} ({res['error']})"
        )
        return JSONResponse(
            status_code=status,
            content={
                "detail": res["error"],
                "outcome": res.get("outcome"),
                **({"projection": res["projection"]} if res.get("projection") else {}),
            },
        )

    record = res["data"]
    print(
        f"[compass] console user={user['id']} {req.action} {record_id} "
        f"-> {record['status']} via {res.get('backend')}"
    )
    return {
        "record": record,
        "action": res.get("action"),
        "outcome": res.get("outcome"),
        "exception_granted": bool(res.get("exception_granted")),
        "backend": res.get("backend"),
    }


# ---------------------------------------------------------------------------
# Documents — the College's calendar, syllabi and regulations
# ---------------------------------------------------------------------------
# library.py re-checks the caller on every mutation (I1); these only shape
# HTTP. The same outcome vocabulary as records: "you may not" is 403, "no
# such document" 404, "already uploaded" / "still processing" 409.
DOCUMENT_OUTCOME_STATUS = {
    "not_authorized": 403,
    "not_found": 404,
    "invalid": 400,
    "empty": 400,
    "not_a_calendar": 400,
    "unsupported_type": 415,
    "too_large": 413,
    "duplicate": 409,
    "still_processing": 409,
    "unavailable": 502,
}


def _library(res: dict, ok_status: int = 200):
    if res["success"]:
        return JSONResponse(status_code=ok_status, content={"outcome": res["outcome"], "data": res["data"]})
    return JSONResponse(
        status_code=DOCUMENT_OUTCOME_STATUS.get(res["outcome"], 400),
        content={"detail": res["error"], "outcome": res["outcome"],
                 **({"document": res["document"]} if res.get("document") else {})},
    )


@app.post("/api/documents")
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    title: str = Form(""),
    branch: str = Form("ALL"),
    semester: str = Form(""),
    academic_year: str = Form(""),
    user: dict = Depends(require_user),
):
    """Upload one document. 202: it is registered and stored, and is
    extracted and indexed in the background — poll GET /api/documents/{id}.

    Read in full before anything is checked, but capped one byte past the
    limit, so an oversized upload is refused without holding all of it.
    """
    data = await file.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    res = library.upload(
        user, file.filename or "upload", data, title=title, doc_type=doc_type,
        branch=branch, semester=semester or None, academic_year=academic_year or None,
    )
    if res["success"]:
        background.add_task(library.ingest, res["data"]["id"])
    return _library(res, ok_status=202)


@app.get("/api/documents")
def list_documents(user: dict = Depends(require_user)):
    res = library.list_documents(user)
    if not res["success"]:
        return _library(res)
    return {"documents": res["data"], "may_manage": auth.may_manage_documents(user)}


@app.get("/api/documents/{document_id}")
def get_document(document_id: str, user: dict = Depends(require_user)):
    return _library(library.get_document(user, document_id))


@app.get("/api/documents/{document_id}/file")
def document_file(document_id: str, user: dict = Depends(require_user)):
    """The original upload, so a citation can be opened and checked."""
    res = library.get_document(user, document_id)
    if not res["success"]:
        return _library(res)
    doc = res["data"]
    path = library.original_path(doc)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The stored file is missing; re-upload it.")
    return FileResponse(path, media_type=doc["content_type"] or "application/octet-stream",
                        filename=doc["filename"], content_disposition_type="inline")


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str, user: dict = Depends(require_user)):
    return _library(library.delete(user, document_id))


@app.get("/api/documents/{document_id}/events")
def calendar_draft(document_id: str, user: dict = Depends(require_user)):
    return _library(library.calendar_draft(user, document_id))


class CalendarReview(BaseModel):
    events: list[dict]


@app.put("/api/documents/{document_id}/events")
def confirm_calendar(document_id: str, req: CalendarReview, user: dict = Depends(require_user)):
    return _library(library.confirm_calendar(user, document_id, req.events))


@app.get("/api/calendar")
def calendar(start: str | None = None, end: str | None = None, user: dict = Depends(require_user)):
    """Confirmed events for the caller's year of study, between two dates
    (default: today onwards). `source` says whether they come from the
    College's confirmed calendar or the placeholder file."""
    try:
        lo = date.fromisoformat(start) if start else clock.today()
        hi = date.fromisoformat(end) if end else None
    except ValueError:
        raise HTTPException(status_code=400, detail="start and end are YYYY-MM-DD.") from None
    res = library.events_in_force(user.get("year"))
    if not res["success"]:
        return _library(res)
    rows = [
        e for e in res["data"]["events"]
        if e["start_date"] and (e["end_date"] or e["start_date"]) >= lo.isoformat()
        and (hi is None or e["start_date"] <= hi.isoformat())
    ]
    return {"source": res["data"]["source"], "events": rows}


@app.post("/api/admin/reindex")
def reindex(user: dict = Depends(require_user)):
    """Rebuild the whole index: the regulations in docs/regulations/ and
    every indexed upload. Document managers only — same rule as uploading."""
    if not auth.may_manage_documents(user):
        raise HTTPException(status_code=403, detail="Only the faculty advisor can rebuild the index.")
    try:
        result = rag.build_index()
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    print(f"[compass] reindex by user={user['id']} -> {result['structured']} chunks")
    return {**result, "status": rag.index_status()}


@app.get("/api/health")
def health():
    """Liveness plus what a demo most needs to know up front."""
    return {
        "backend": backends.get_backend().name,
        "index": rag.index_status(),
        "tracker": ({"enabled": True, "repo": config.GITHUB_REPO, "pending": len(tracker.pending())}
                    if tracker.enabled() else {"enabled": False}),
    }


# ---------------------------------------------------------------------------
# Frontend — the built React app, when there is one
# ---------------------------------------------------------------------------
if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    index = WEB_DIST / "index.html"
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="No such endpoint.")
    if not index.exists():
        return JSONResponse(
            status_code=503,
            content={"detail": "Frontend not built. Run: cd web && npm install && npm run build "
                               "(or use the Vite dev server on :5173)."},
        )
    # no-cache on the shell only: it names the hashed asset files, so a stale
    # copy keeps loading the previous build's JavaScript after a rebuild.
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
