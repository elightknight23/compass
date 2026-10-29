"""JsonBackend — files under data/ as the system of record.

The zero-setup backend, and the fallback when Postgres is unreachable.

    data/records.json      complaints and leave requests (live; seeded from
                           records.json.seed by scripts/reset_demo.py)
    data/events.json       the audit trail (live; starts empty)
    data/users.json        the directory — served to browsers, so no secrets
    data/credentials.json  username -> scrypt hash, kept apart for that reason
    data/attendance.json   per-student attended/held periods
    data/documents.json    uploaded documents (live; starts empty)
    data/calendar.json     events parsed from uploaded calendars (live)

Writes are read-modify-rewrite of the whole file, which is fine at this
size and honest about what it is. The one concession to safety is that the
rewrite is atomic — serialize to a sibling temp file, then os.replace() it
over the original. A plain json.dump() truncates the file first, so a crash
(or a Ctrl-C during the demo) mid-write leaves it half-written and
unparseable.

Concurrency is not handled: two simultaneous writes can still lose one
update, and a record write and its event write are two files, not one
transaction. Postgres is where both of those are solved properly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import clock, config
from . import (
    COMPLAINT,
    COMPLAINT_PREFIX,
    DOCUMENT_MUTABLE_FIELDS,
    DOCUMENT_PREFIX,
    DOCUMENT_START,
    INVALID,
    MUTABLE_FIELDS,
    NOT_FOUND,
    OD_PREFIX,
    OD_REQUEST,
    UNAVAILABLE,
    blank_document,
    blank_event,
    blank_record,
    fail,
    ok,
)


class JsonBackend:
    """Implements RecordBackend over plain files."""

    name = "json"

    def __init__(self, data_dir: Path | None = None) -> None:
        self.dir = data_dir or config.DATA_DIR
        self.records_path = self.dir / "records.json"
        self.events_path = self.dir / "events.json"
        self.documents_path = self.dir / "documents.json"
        self.calendar_path = self.dir / "calendar.json"

    # -- file I/O ----------------------------------------------------------
    def _load(self, path: Path, default: Any = None) -> Any:
        if default is not None and not path.exists():
            return default
        return json.loads(path.read_text())

    def _save(self, path: Path, payload: Any) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, path)  # atomic on POSIX; never a partial file

    def _records(self) -> list[dict]:
        # Normalise on read, so a seed file that omits a key still yields
        # records with every key present.
        return [blank_record(**r) for r in self._load(self.records_path)]

    @staticmethod
    def _next_id(records: list[dict], prefix: str, start: int) -> str:
        used = [
            int(r["id"][len(prefix):])
            for r in records
            if str(r.get("id", "")).startswith(prefix)
            and r["id"][len(prefix):].isdigit()
        ]
        return f"{prefix}{max(used, default=start - 1) + 1}"

    def _append(self, record: dict) -> dict:
        try:
            records = self._records()
            record["id"] = self._next_id(
                records,
                COMPLAINT_PREFIX if record["type"] == COMPLAINT else OD_PREFIX,
                1001 if record["type"] == COMPLAINT else 2001,
            )
            records.append(record)
            self._save(self.records_path, records)
        except (OSError, ValueError) as e:
            return fail(f"Could not write to the records file: {e}", UNAVAILABLE)
        return ok(record, backend=self.name)

    # -- records -----------------------------------------------------------
    def create_complaint(
        self,
        user_id: str,
        notes: str,
        category: str = "hostel",
        priority: str = "medium",
    ) -> dict:
        return self._append(
            blank_record(
                user_id=user_id,
                type=COMPLAINT,
                status="open",
                created_at=clock.today().isoformat(),
                notes=notes,
                category=category,
                priority=priority,
            )
        )

    def create_od_request(
        self,
        user_id: str,
        leave_kind: str,
        start_date: str,
        end_date: str,
        working_days: int,
        reason: str,
        projection: dict | None,
        needs_exception: bool,
    ) -> dict:
        return self._append(
            blank_record(
                user_id=user_id,
                type=OD_REQUEST,
                status="submitted",
                created_at=clock.today().isoformat(),
                notes=reason,
                leave_kind=leave_kind,
                start_date=start_date,
                end_date=end_date,
                working_days=working_days,
                reason=reason,
                projection=projection,
                needs_exception=bool(needs_exception),
            )
        )

    def get_record(self, record_id: str) -> dict:
        try:
            records = self._records()
        except (OSError, ValueError) as e:
            return fail(f"Could not read the records file: {e}", UNAVAILABLE)
        wanted = (record_id or "").strip().upper()
        match = next((r for r in records if r["id"].upper() == wanted), None)
        return ok(match, backend=self.name)  # ok(None) == no such record

    def update_record(self, record_id: str, **fields: Any) -> dict:
        unknown = set(fields) - MUTABLE_FIELDS
        if unknown:
            return fail(f"Fields not updatable: {', '.join(sorted(unknown))}.", INVALID)
        try:
            records = self._records()
            wanted = (record_id or "").strip().upper()
            match = next((r for r in records if r["id"].upper() == wanted), None)
            if match is None:
                return fail(f"No record {record_id} exists.", NOT_FOUND)
            match.update(fields)
            self._save(self.records_path, records)
        except (OSError, ValueError) as e:
            return fail(f"Could not write to the records file: {e}", UNAVAILABLE)
        return ok(match, backend=self.name)

    def list_records_for_user(
        self, user_id: str, types: tuple[str, ...] | None = None
    ) -> dict:
        try:
            records = self._records()
        except (OSError, ValueError) as e:
            return fail(f"Could not read the records file: {e}", UNAVAILABLE)
        hits = [
            r for r in records
            if r["user_id"] == user_id and (types is None or r["type"] in types)
        ]
        return ok(hits, backend=self.name)

    def list_all_records(self, types: tuple[str, ...] | None = None) -> dict:
        try:
            records = self._records()
        except (OSError, ValueError) as e:
            return fail(f"Could not read the records file: {e}", UNAVAILABLE)
        hits = [r for r in records if types is None or r["type"] in types]
        hits.sort(key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)
        return ok(hits, backend=self.name)

    # -- audit trail -------------------------------------------------------
    def append_event(
        self,
        record_id: str,
        actor_id: str,
        verb: str,
        from_status: str | None,
        to_status: str | None,
        note: str | None = None,
    ) -> dict:
        try:
            events = self._load(self.events_path, default=[])
            event = {
                "id": len(events) + 1,
                "record_id": record_id,
                "actor_id": actor_id,
                "verb": verb,
                "from_status": from_status,
                "to_status": to_status,
                "note": note,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            events.append(event)
            self._save(self.events_path, events)
        except (OSError, ValueError) as e:
            return fail(f"Could not write to the events file: {e}", UNAVAILABLE)
        return ok(event, backend=self.name)

    def list_events(self, record_id: str) -> dict:
        try:
            events = self._load(self.events_path, default=[])
        except (OSError, ValueError) as e:
            return fail(f"Could not read the events file: {e}", UNAVAILABLE)
        wanted = (record_id or "").strip().upper()
        return ok(
            [e for e in events if str(e["record_id"]).upper() == wanted],
            backend=self.name,
        )

    # -- people and attendance --------------------------------------------
    def list_users(self) -> dict:
        try:
            return ok(self._load(self.dir / "users.json"), backend=self.name)
        except (OSError, ValueError) as e:
            return fail(f"Could not read the user directory: {e}", UNAVAILABLE)

    def get_credentials(self, username: str) -> dict:
        try:
            creds = self._load(self.dir / "credentials.json")
        except (OSError, ValueError) as e:
            return fail(f"Could not read credentials: {e}", UNAVAILABLE)
        entry = creds.get((username or "").strip().lower())
        if entry is None:
            return ok(None, backend=self.name)
        return ok(
            {"user_id": entry["user_id"], "password_hash": entry["password"]},
            backend=self.name,
        )

    def get_attendance(self, user_id: str) -> dict:
        try:
            rows = self._load(self.dir / "attendance.json")
        except (OSError, ValueError) as e:
            return fail(f"Could not read attendance: {e}", UNAVAILABLE)
        row = next((r for r in rows if r["user_id"] == user_id), None)
        if row is None:
            return ok(None, backend=self.name)
        return ok(
            {"attended": row["attended"], "held": row["held"], "as_of": row.get("as_of")},
            backend=self.name,
        )

    # -- uploaded documents ------------------------------------------------
    def _documents(self) -> list[dict]:
        return [blank_document(**d) for d in self._load(self.documents_path, default=[])]

    def create_document(self, **fields: Any) -> dict:
        try:
            docs = self._documents()
            doc = blank_document(**fields)
            doc["id"] = self._next_id(docs, DOCUMENT_PREFIX, DOCUMENT_START)
            docs.append(doc)
            self._save(self.documents_path, docs)
        except (OSError, ValueError) as e:
            return fail(f"Could not write to the documents file: {e}", UNAVAILABLE)
        return ok(doc, backend=self.name)

    def get_document(self, document_id: str) -> dict:
        try:
            docs = self._documents()
        except (OSError, ValueError) as e:
            return fail(f"Could not read the documents file: {e}", UNAVAILABLE)
        wanted = (document_id or "").strip().upper()
        return ok(next((d for d in docs if d["id"].upper() == wanted), None), backend=self.name)

    def update_document(self, document_id: str, **fields: Any) -> dict:
        unknown = set(fields) - DOCUMENT_MUTABLE_FIELDS
        if unknown:
            return fail(f"Fields not updatable: {', '.join(sorted(unknown))}.", INVALID)
        try:
            docs = self._documents()
            wanted = (document_id or "").strip().upper()
            match = next((d for d in docs if d["id"].upper() == wanted), None)
            if match is None:
                return fail(f"No document {document_id} exists.", NOT_FOUND)
            match.update(fields)
            self._save(self.documents_path, docs)
        except (OSError, ValueError) as e:
            return fail(f"Could not write to the documents file: {e}", UNAVAILABLE)
        return ok(match, backend=self.name)

    def list_documents(self) -> dict:
        try:
            docs = self._documents()
        except (OSError, ValueError) as e:
            return fail(f"Could not read the documents file: {e}", UNAVAILABLE)
        docs.sort(key=lambda d: (d.get("uploaded_at") or "", d["id"]), reverse=True)
        return ok(docs, backend=self.name)

    def delete_document(self, document_id: str) -> dict:
        try:
            docs = self._documents()
            wanted = (document_id or "").strip().upper()
            match = next((d for d in docs if d["id"].upper() == wanted), None)
            if match is None:
                return fail(f"No document {document_id} exists.", NOT_FOUND)
            # Events first: a crash between the two writes leaves a document
            # without events (re-uploadable), never events without a document.
            events = self._load(self.calendar_path, default=[])
            self._save(self.calendar_path,
                       [e for e in events if str(e["document_id"]).upper() != wanted])
            self._save(self.documents_path, [d for d in docs if d is not match])
        except (OSError, ValueError) as e:
            return fail(f"Could not write to the documents file: {e}", UNAVAILABLE)
        return ok(match, backend=self.name)

    # -- the calendar ------------------------------------------------------
    def replace_calendar_events(self, document_id: str, events: list[dict]) -> dict:
        try:
            current = self._load(self.calendar_path, default=[])
            wanted = (document_id or "").strip().upper()
            kept = [e for e in current if str(e["document_id"]).upper() != wanted]
            next_id = max((int(e["id"]) for e in current), default=0) + 1
            stored = []
            for i, e in enumerate(events):
                row = blank_event(**e)
                row["id"] = next_id + i
                row["document_id"] = document_id
                stored.append(row)
            self._save(self.calendar_path, kept + stored)
        except (OSError, ValueError) as e:
            return fail(f"Could not write to the calendar file: {e}", UNAVAILABLE)
        return ok(stored, backend=self.name)

    def list_calendar_events(
        self, confirmed_only: bool = True, document_id: str | None = None
    ) -> dict:
        try:
            events = [blank_event(**e) for e in self._load(self.calendar_path, default=[])]
        except (OSError, ValueError) as e:
            return fail(f"Could not read the calendar file: {e}", UNAVAILABLE)
        if confirmed_only:
            events = [e for e in events if e["confirmed"]]
        if document_id is not None:
            wanted = document_id.strip().upper()
            events = [e for e in events if str(e["document_id"]).upper() == wanted]
        # Undated rows (a holiday with "date to be announced") last, as in SQL.
        events.sort(key=lambda e: (e["start_date"] is None, e["start_date"] or "", e["id"]))
        return ok(events, backend=self.name)
