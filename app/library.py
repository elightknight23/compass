"""The College's documents — upload, process, review, remove.

This is the handler layer for documents, as handlers.py is for records,
and it follows the same rules:

- **Every mutation checks its caller itself** (I1), first, before reading
  or writing anything — the endpoint's own check is a courtesy. Only a
  document manager (auth.DOCUMENT_MANAGERS) may add, remove or confirm,
  because an upload is read by every student's assistant: it is a write
  into everyone's answers.
- **Outcomes, not exceptions.** Each function returns the usual envelope
  plus an `outcome`, and main.py maps outcomes to HTTP statuses. "You may
  not", "no such document", "already uploaded" and "still processing" are
  different answers.

The pipeline for one upload:

    upload()   validate -> register (status processing) -> save the original
    ingest()   extract -> normalise to markdown -> index -> for a calendar,
               parse draft events -> status indexed | needs_review | failed
    confirm_calendar()
               the advisor's reviewed rows become the calendar in force;
               only now can a holiday change a working-day count

ingest() runs after the upload response, on a background task, because a
long syllabus takes a while to embed. A server that dies mid-ingest leaves
the document in `processing`; recover_interrupted() marks those failed at
startup so they can be deleted and re-uploaded rather than stuck forever.
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import auth, backends, config, dates, documents, rag
from .backends import DOC_TYPES, EVENT_KINDS

MAX_EVENT_SPAN_DAYS = 120  # a "holiday" longer than a semester is a typo


def _log(message: str) -> None:
    print(f"[compass-library] {message}", file=sys.stderr, flush=True)


def _done(data=None, outcome: str = "ok", **extra) -> dict:
    return {"success": True, "data": data, "error": None, "outcome": outcome, **extra}


def _refused(error: str, outcome: str, **extra) -> dict:
    return {"success": False, "data": None, "error": error, "outcome": outcome, **extra}


def _not_manager() -> dict:
    return _refused(
        "Only the faculty advisor can add, remove or confirm the College's "
        "documents — what is uploaded here is what every student's answers "
        "come from.",
        "not_authorized",
    )


def original_path(doc: dict) -> Path:
    ext = Path(doc.get("filename") or "").suffix.lower()
    return config.UPLOADS_DIR / f"{doc['id']}{ext}"


# ---------------------------------------------------------------------------
# Reading — any signed-in user
# ---------------------------------------------------------------------------
SEARCHABLE = rag.INDEXED_STATUSES


def list_documents(user: dict) -> dict:
    """Managers see everything, including failures they need to deal with;
    everyone else sees what their answers can come from."""
    res = backends.get_backend().list_documents()
    if not res["success"]:
        return _refused(res["error"], "unavailable")
    docs = res["data"]
    if not auth.may_manage_documents(user):
        docs = [d for d in docs if d["status"] in SEARCHABLE]
    return _done(docs)


def get_document(user: dict, document_id: str) -> dict:
    res = backends.get_backend().get_document(document_id)
    if not res["success"]:
        return _refused(res["error"], "unavailable")
    doc = res["data"]
    # A document a student cannot see does not exist, as far as they know.
    if doc is None or (doc["status"] not in SEARCHABLE and not auth.may_manage_documents(user)):
        return _refused(f"No document {document_id} exists.", "not_found")
    return _done(doc)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def _clean_meta(filename: str, title: str | None, doc_type: str | None, branch: str | None,
                semester, academic_year: str | None) -> dict | str:
    """Validated metadata, or the sentence explaining what is wrong."""
    if doc_type not in DOC_TYPES:
        return f"Document type must be one of: {', '.join(DOC_TYPES)}."
    title = re.sub(r"\s+", " ", (title or "").strip()) or Path(filename or "document").stem
    if len(title) > 200:
        return "The title is too long (200 characters at most)."
    code = documents.normalize_branch(branch) if branch not in (None, "") else "ALL"
    if not code:
        return "That branch name has no letters or digits in it."
    sem = None
    if semester not in (None, "", 0, "0"):
        try:
            sem = int(semester)
        except (TypeError, ValueError):
            return "Semester must be a number from 1 to 10, or left blank for all."
        if not 1 <= sem <= 10:
            return "Semester must be a number from 1 to 10, or left blank for all."
    year = (academic_year or "").strip() or None
    if year and documents.academic_years(year) is None:
        return 'Academic year should look like "2026-27".'
    if doc_type == "academic_calendar" and year is None:
        return ('An academic calendar needs its academic year (like "2026-27"): many '
                "calendars print dates without a year, and it says which year they fall in.")
    return {"title": title, "doc_type": doc_type, "branch": code, "semester": sem,
            "academic_year": year}


def upload(user: dict, filename: str, data: bytes, *, title: str | None = None,
           doc_type: str | None = None, branch: str | None = None, semester=None,
           academic_year: str | None = None) -> dict:
    """Register and store an upload. Processing happens in ingest()."""
    if not auth.may_manage_documents(user):
        return _not_manager()

    meta = _clean_meta(filename, title, doc_type, branch, semester, academic_year)
    if isinstance(meta, str):
        return _refused(meta, "invalid")
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    if len(data) > limit:
        return _refused(f"That file is larger than {config.MAX_UPLOAD_MB} MB.", "too_large")
    try:
        ext = documents.sniff(filename, data)
    except documents.DocumentError as e:
        return _refused(str(e), e.kind)

    backend = backends.get_backend()
    digest = hashlib.sha256(data).hexdigest()
    existing = backend.list_documents()
    if not existing["success"]:
        return _refused(existing["error"], "unavailable")
    twin = next((d for d in existing["data"] if d["sha256"] == digest and d["status"] != "failed"), None)
    if twin:
        return _refused(f"That exact file is already uploaded as {twin['id']} ({twin['title']}).",
                        "duplicate", document=twin)

    res = backend.create_document(
        **meta,
        filename=Path(filename).name[:255],
        content_type=documents.SUPPORTED[ext],
        sha256=digest,
        size=len(data),
        status="processing",
        uploaded_by=user["id"],
        uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if not res["success"]:
        return _refused(res["error"], "unavailable")
    doc = res["data"]
    try:
        config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        original_path(doc).write_bytes(data)
    except OSError as e:
        backend.delete_document(doc["id"])
        return _refused(f"The file could not be stored ({type(e).__name__}).", "unavailable")
    _log(f"{user['id']} uploaded {doc['id']} {doc['doc_type']} {doc['title']!r} ({len(data)} bytes)")
    return _done(doc, "accepted")


def ingest(document_id: str) -> dict:
    """Extract, normalise, index, and (for a calendar) draft its events.

    Never raises: every failure lands on the document as status `failed`
    with a sentence the uploader can act on, and any chunks already added
    are taken back out so a half-indexed file never answers questions.
    """
    backend = backends.get_backend()
    res = backend.get_document(document_id)
    if not res["success"] or res["data"] is None:
        return _refused(f"No document {document_id} to process.", "not_found")
    doc = res["data"]

    def failed(message: str) -> dict:
        rag.remove_document(doc["id"])
        backend.replace_calendar_events(doc["id"], [])
        rag.upload_text_path(doc["id"]).unlink(missing_ok=True)
        backend.update_document(doc["id"], status="failed", error=message, chunks=0)
        rag.stamp_index()
        _log(f"{doc['id']} failed: {message}")
        return _refused(message, "failed")

    try:
        data = original_path(doc).read_bytes()
        ext = documents.sniff(doc["filename"], data)
        extracted = documents.extract(data, ext, max_pages=config.MAX_UPLOAD_PAGES)
        markdown = documents.to_markdown(extracted, doc["doc_type"], doc["title"])
        rag.upload_text_path(doc["id"]).write_text(markdown)
        chunks = rag.index_document(doc, markdown)
        if chunks == 0:
            return failed("No text could be indexed from that file.")

        status, note = "indexed", None
        if doc["doc_type"] == "academic_calendar":
            events = documents.parse_calendar(extracted, doc.get("academic_year"))
            stored = backend.replace_calendar_events(doc["id"], events)
            if not stored["success"]:
                return failed(f"The calendar's dates could not be saved ({stored['error']}).")
            if events:
                status = "needs_review"
            else:
                note = ("Searchable, but no dated events were found in it, so it "
                        "can't answer holiday questions or change working days.")
        backend.update_document(doc["id"], status=status, error=note, chunks=chunks,
                                pages=extracted.pages)
        rag.stamp_index()
        _log(f"{doc['id']} {status}: {chunks} chunks")
        return _done(backend.get_document(doc["id"])["data"], status)
    except documents.DocumentError as e:
        return failed(str(e))
    except Exception as e:  # noqa: BLE001 — a background task must not die silently
        return failed(f"Processing failed unexpectedly ({type(e).__name__}). Try uploading it again.")


def recover_interrupted() -> int:
    """Startup: anything still `processing` was interrupted by a restart."""
    backend = backends.get_backend()
    res = backend.list_documents()
    n = 0
    for doc in res["data"] or [] if res["success"] else []:
        if doc["status"] == "processing":
            rag.remove_document(doc["id"])
            backend.update_document(
                doc["id"], status="failed",
                error="The server restarted while this was being processed. Delete it and upload it again.",
            )
            n += 1
    if n:
        rag.stamp_index()
        _log(f"marked {n} interrupted upload(s) failed")
    return n


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def delete(user: dict, document_id: str) -> dict:
    if not auth.may_manage_documents(user):
        return _not_manager()
    backend = backends.get_backend()
    res = backend.get_document(document_id)
    if not res["success"]:
        return _refused(res["error"], "unavailable")
    doc = res["data"]
    if doc is None:
        return _refused(f"No document {document_id} exists.", "not_found")
    if doc["status"] == "processing":
        # The background ingest would re-add its chunks after we removed them.
        return _refused(f"{doc['id']} is still being processed; delete it once it finishes.",
                        "still_processing")

    rag.remove_document(doc["id"])
    gone = backend.delete_document(doc["id"])
    if not gone["success"]:
        return _refused(gone["error"], "unavailable")
    for path in (original_path(doc), rag.upload_text_path(doc["id"])):
        path.unlink(missing_ok=True)
    dates.refresh_holidays()  # its confirmed holidays, if any, are gone too
    rag.stamp_index()
    _log(f"{user['id']} deleted {doc['id']} {doc['title']!r}")
    return _done(doc, "deleted")


# ---------------------------------------------------------------------------
# Calendar review
# ---------------------------------------------------------------------------
def _calendar_doc(user: dict, document_id: str) -> dict:
    if not auth.may_manage_documents(user):
        return _not_manager()
    res = backends.get_backend().get_document(document_id)
    if not res["success"]:
        return _refused(res["error"], "unavailable")
    doc = res["data"]
    if doc is None:
        return _refused(f"No document {document_id} exists.", "not_found")
    if doc["doc_type"] != "academic_calendar":
        return _refused(f"{doc['id']} is not an academic calendar.", "not_a_calendar")
    if doc["status"] in ("processing", "failed"):
        return _refused(f"{doc['id']} is {doc['status']}; there is nothing to review.",
                        "still_processing" if doc["status"] == "processing" else "invalid")
    return _done(doc)


def calendar_draft(user: dict, document_id: str) -> dict:
    """Every event parsed from one calendar, confirmed or not, for review."""
    got = _calendar_doc(user, document_id)
    if not got["success"]:
        return got
    res = backends.get_backend().list_calendar_events(confirmed_only=False, document_id=document_id)
    if not res["success"]:
        return _refused(res["error"], "unavailable")
    return _done({"document": got["data"], "events": res["data"]})


def _iso(value) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{value!r} is not a date (YYYY-MM-DD)") from None


def _clean_years(value) -> str:
    if value in (None, "", "all") or value == ["all"]:
        return "all"
    items = value if isinstance(value, list) else str(value).split(",")
    years = sorted({int(str(x).strip()) for x in items if str(x).strip()})
    if not years or any(not 1 <= y <= 5 for y in years):
        raise ValueError("years must be 'all' or year numbers from 1 to 5")
    return ",".join(map(str, years))


def confirm_calendar(user: dict, document_id: str, rows: list[dict]) -> dict:
    """The advisor's reviewed rows become the calendar in force.

    A row is in force when it is ticked (`include`) and dated. Dates here
    are what a person checked against the document — the parser proposed
    them, the reviewer decides. Unticked rows are kept, unconfirmed, so
    the review can be reopened and changed later.
    """
    got = _calendar_doc(user, document_id)
    if not got["success"]:
        return got
    if not isinstance(rows, list):
        return _refused("Send the reviewed rows as a list.", "invalid")

    cleaned = []
    for n, row in enumerate(rows, start=1):
        try:
            start = _iso(row.get("start_date"))
            end = _iso(row.get("end_date")) or start
            include = bool(row.get("include"))
            title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
            kind = row.get("kind") or "other"
            if kind not in EVENT_KINDS:
                raise ValueError(f"kind must be one of {', '.join(EVENT_KINDS)}")
            if include and not title:
                raise ValueError("it has no title")
            if include and start is None:
                raise ValueError("it is ticked but has no date — add one or untick it")
            if start is None and row.get("end_date"):
                raise ValueError("it has an end date but no start date")
            if start and end and end < start:
                raise ValueError("it ends before it starts")
            if start and end and (end - start).days > MAX_EVENT_SPAN_DAYS:
                raise ValueError(f"it spans more than {MAX_EVENT_SPAN_DAYS} days")
            years = _clean_years(row.get("years"))
        except ValueError as e:
            return _refused(f"Row {n} ({row.get('title') or 'untitled'}): {e}.", "invalid", row=n)
        cleaned.append({
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "title": title[:200] or "(untitled)",
            "kind": kind,
            "years": years,
            "source_text": (row.get("source_text") or "")[:500] or None,
            "page": row.get("page") if isinstance(row.get("page"), int) else None,
            "flags": [f for f in (row.get("flags") or []) if isinstance(f, str)][:10],
            "include": include,
            "confirmed": include and start is not None,
        })

    backend = backends.get_backend()
    stored = backend.replace_calendar_events(document_id, cleaned)
    if not stored["success"]:
        return _refused(stored["error"], "unavailable")
    backend.update_document(document_id, status="indexed", error=None)
    dates.refresh_holidays()
    in_force = [e for e in stored["data"] if e["confirmed"]]
    holidays = [e for e in in_force if e["kind"] == "holiday"]
    _log(f"{user['id']} confirmed {document_id}: {len(in_force)} events, {len(holidays)} holidays")
    return _done({"events": stored["data"], "confirmed": len(in_force), "holidays": len(holidays)},
                 "confirmed")


# ---------------------------------------------------------------------------
# The calendar in force — what students are told and what arithmetic uses
# ---------------------------------------------------------------------------
def events_in_force(year: int | None = None) -> dict:
    """ok({"source", "events"}): confirmed events for this year of study,
    or, while no calendar has been confirmed, the placeholder holidays —
    labelled as such, so no reply passes them off as the College's."""
    res = backends.get_backend().list_calendar_events(confirmed_only=True)
    if not res["success"]:
        return _refused(res["error"], "unavailable")
    if res["data"]:
        events = [e for e in res["data"] if e["include"] and dates.event_applies(e, year)]
        return _done({"source": "calendar", "events": events})
    return _done({"source": "seed", "events": dates.seed_holidays()})
