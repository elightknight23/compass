"""PostgresBackend — the system of record, through SQLAlchemy.

Implements the same RecordBackend protocol as JsonBackend, and is tested
against the same protocol checks (scripts/test_backends.py). The handlers
cannot tell the two apart, which is the whole point of the boundary: if the
database is unreachable during a demo, RECORD_BACKEND=json and a restart is
the entire recovery.

What Postgres buys over the JSON files is what those files admit they
lack: each write is a transaction, so two simultaneous writers cannot
silently lose one another's update, and ids cannot collide. (A status
change and its audit event are still two protocol calls, and so two
transactions. Folding them into one would mean a backend method that
knows about staff decisions, which is a handler's business.)

Every database error becomes a fail(...) envelope with kind UNAVAILABLE.
The user hears "I couldn't reach the records system", never a traceback —
and never a record id that was not actually written.

The URL is any SQLAlchemy URL. Production is postgresql+psycopg://...; the
offline test suite runs the same class against a throwaway SQLite file, so
the protocol tests need no running server.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .. import clock, config
from ..models import Attendance, Base, CalendarEvent, Document, MessMenu, Record, RecordEvent, User
from . import (
    CALENDAR_EVENT_FIELDS,
    COMPLAINT,
    COMPLAINT_PREFIX,
    DOCUMENT_FIELDS,
    DOCUMENT_MUTABLE_FIELDS,
    DOCUMENT_PREFIX,
    DOCUMENT_START,
    INVALID,
    MUTABLE_FIELDS,
    NOT_FOUND,
    OD_PREFIX,
    OD_REQUEST,
    RECORD_FIELDS,
    UNAVAILABLE,
    fail,
    ok,
)

PUBLIC_USER_COLUMNS = (
    "id", "username", "name", "role", "department", "branch", "year",
    "semester", "section", "mess", "residence", "context",
)


def _as_dict(row: Record) -> dict:
    return {k: getattr(row, k) for k in RECORD_FIELDS}


def _doc_dict(d: Document) -> dict:
    return {k: getattr(d, k) for k in DOCUMENT_FIELDS}


def _cal_dict(e: CalendarEvent) -> dict:
    return {k: getattr(e, k) for k in CALENDAR_EVENT_FIELDS}


def _event_dict(e: RecordEvent) -> dict:
    return {
        "id": e.id,
        "record_id": e.record_id,
        "actor_id": e.actor_id,
        "verb": e.verb,
        "from_status": e.from_status,
        "to_status": e.to_status,
        "note": e.note,
        "at": e.at.isoformat(timespec="seconds") if e.at else None,
    }


def make_engine(url: str):
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        # Fail in three seconds, not the driver's default of "eventually":
        # a demo waiting on a dead database is a dead demo, and the JSON
        # fallback is one environment variable away.
        kwargs["connect_args"] = {"connect_timeout": 3}
    return create_engine(url, **kwargs)


def init_schema(engine) -> None:
    """Create the tables if they do not exist. Used by seeding and tests,
    never implicitly by the backend — a server that silently creates an
    empty schema on a typo'd URL looks healthy and has no data."""
    Base.metadata.create_all(engine)


class PostgresBackend:
    """Implements RecordBackend over SQLAlchemy."""

    name = "postgres"

    def __init__(self, url: str | None = None) -> None:
        self.url = url or config.DATABASE_URL
        self.engine = make_engine(self.url)
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def _db(self) -> Session:
        return self._session()

    # Ids are "the highest so far, plus one", read and written in one
    # transaction — so two writers at the same moment compute the same id,
    # and the primary key refuses one. Measured: 27 of 40 simultaneous
    # filings failed with two retries. Within one server process the lock
    # makes them take turns (a few milliseconds each); across processes,
    # jittered retries settle the rest.
    _ALLOCATE = threading.Lock()
    ALLOCATE_ATTEMPTS = 8

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(random.uniform(0.005, 0.02) * (attempt + 1))

    @staticmethod
    def _unavailable(e: Exception) -> dict:
        # The message may reach a user, so it names the failure, not the URL
        # (which can carry a password).
        return fail(f"the database could not be reached ({type(e).__name__})", UNAVAILABLE)

    # -- records -----------------------------------------------------------
    @staticmethod
    def _next_id(db: Session, column, prefix: str, start: int) -> str:
        ids = db.scalars(select(column).where(column.like(f"{prefix}%"))).all()
        used = [int(i[len(prefix):]) for i in ids if i[len(prefix):].isdigit()]
        return f"{prefix}{max(used, default=start - 1) + 1}"

    @staticmethod
    def _find_record(db: Session, record_id: str) -> Record | None:
        return db.scalar(
            select(Record).where(func.upper(Record.id) == (record_id or "").strip().upper())
        )

    def _insert(self, record_type: str, **fields: Any) -> dict:
        prefix, start = (
            (COMPLAINT_PREFIX, 1001) if record_type == COMPLAINT else (OD_PREFIX, 2001)
        )
        for attempt in range(self.ALLOCATE_ATTEMPTS):
            try:
                with self._ALLOCATE, self._db() as db, db.begin():
                    row = Record(
                        id=self._next_id(db, Record.id, prefix, start),
                        type=record_type,
                        created_at=clock.today().isoformat(),
                        **fields,
                    )
                    db.add(row)
                return ok(_as_dict(row), backend=self.name)
            except IntegrityError:
                self._backoff(attempt)
                continue
            except SQLAlchemyError as e:
                return self._unavailable(e)
        return fail("could not allocate a record id; please retry", UNAVAILABLE)

    def create_complaint(
        self, user_id: str, notes: str, category: str = "hostel", priority: str = "medium"
    ) -> dict:
        return self._insert(
            COMPLAINT, user_id=user_id, status="open", notes=notes,
            category=category, priority=priority,
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
        return self._insert(
            OD_REQUEST, user_id=user_id, status="submitted", notes=reason,
            leave_kind=leave_kind, start_date=start_date, end_date=end_date,
            working_days=working_days, reason=reason, projection=projection,
            needs_exception=bool(needs_exception),
        )

    def get_record(self, record_id: str) -> dict:
        try:
            with self._db() as db:
                row = self._find_record(db, record_id)
                return ok(_as_dict(row) if row else None, backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def update_record(self, record_id: str, **fields: Any) -> dict:
        unknown = set(fields) - MUTABLE_FIELDS
        if unknown:
            return fail(f"Fields not updatable: {', '.join(sorted(unknown))}.", INVALID)
        try:
            with self._db() as db, db.begin():
                row = self._find_record(db, record_id)
                if row is None:
                    return fail(f"No record {record_id} exists.", NOT_FOUND)
                for k, v in fields.items():
                    setattr(row, k, v)
            return ok(_as_dict(row), backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def _list(self, stmt) -> dict:
        try:
            with self._db() as db:
                return ok([_as_dict(r) for r in db.scalars(stmt).all()], backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def list_records_for_user(self, user_id: str, types: tuple[str, ...] | None = None) -> dict:
        stmt = select(Record).where(Record.user_id == user_id)
        if types is not None:
            stmt = stmt.where(Record.type.in_(types))
        return self._list(stmt.order_by(Record.created_at, Record.id))

    def list_all_records(self, types: tuple[str, ...] | None = None) -> dict:
        stmt = select(Record)
        if types is not None:
            stmt = stmt.where(Record.type.in_(types))
        return self._list(stmt.order_by(Record.created_at.desc(), Record.id.desc()))

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
            with self._db() as db, db.begin():
                event = RecordEvent(
                    record_id=record_id, actor_id=actor_id, verb=verb,
                    from_status=from_status, to_status=to_status, note=note,
                )
                db.add(event)
            return ok(_event_dict(event), backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def list_events(self, record_id: str) -> dict:
        try:
            with self._db() as db:
                rows = db.scalars(
                    select(RecordEvent)
                    .where(func.upper(RecordEvent.record_id) == (record_id or "").strip().upper())
                    .order_by(RecordEvent.id)
                ).all()
                return ok([_event_dict(e) for e in rows], backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    # -- people and attendance --------------------------------------------
    def list_users(self) -> dict:
        try:
            with self._db() as db:
                rows = db.scalars(select(User).order_by(User.id)).all()
                # password_hash is never selected into a dict that leaves here.
                return ok(
                    [{k: getattr(u, k) for k in PUBLIC_USER_COLUMNS} for u in rows],
                    backend=self.name,
                )
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def get_credentials(self, username: str) -> dict:
        try:
            with self._db() as db:
                u = db.scalar(select(User).where(User.username == (username or "").strip().lower()))
                if u is None:
                    return ok(None, backend=self.name)
                return ok({"user_id": u.id, "password_hash": u.password_hash}, backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def get_attendance(self, user_id: str) -> dict:
        try:
            with self._db() as db:
                a = db.get(Attendance, user_id)
                if a is None:
                    return ok(None, backend=self.name)
                return ok(
                    {"attended": a.attended, "held": a.held, "as_of": a.as_of},
                    backend=self.name,
                )
        except SQLAlchemyError as e:
            return self._unavailable(e)

    # -- uploaded documents ------------------------------------------------
    def _find_doc(self, db: Session, document_id: str) -> Document | None:
        return db.scalar(
            select(Document).where(func.upper(Document.id) == (document_id or "").strip().upper())
        )

    def create_document(self, **fields: Any) -> dict:
        fields = {k: v for k, v in fields.items() if k in DOCUMENT_FIELDS and k != "id"}
        for attempt in range(self.ALLOCATE_ATTEMPTS):  # same id race as _insert
            try:
                with self._ALLOCATE, self._db() as db, db.begin():
                    row = Document(id=self._next_id(db, Document.id, DOCUMENT_PREFIX, DOCUMENT_START),
                                   **fields)
                    db.add(row)
                return ok(_doc_dict(row), backend=self.name)
            except IntegrityError:
                self._backoff(attempt)
                continue
            except SQLAlchemyError as e:
                return self._unavailable(e)
        return fail("could not allocate a document id; please retry", UNAVAILABLE)

    def get_document(self, document_id: str) -> dict:
        try:
            with self._db() as db:
                row = self._find_doc(db, document_id)
                return ok(_doc_dict(row) if row else None, backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def update_document(self, document_id: str, **fields: Any) -> dict:
        unknown = set(fields) - DOCUMENT_MUTABLE_FIELDS
        if unknown:
            return fail(f"Fields not updatable: {', '.join(sorted(unknown))}.", INVALID)
        try:
            with self._db() as db, db.begin():
                row = self._find_doc(db, document_id)
                if row is None:
                    return fail(f"No document {document_id} exists.", NOT_FOUND)
                for k, v in fields.items():
                    setattr(row, k, v)
            return ok(_doc_dict(row), backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def list_documents(self) -> dict:
        try:
            with self._db() as db:
                rows = db.scalars(
                    select(Document).order_by(Document.uploaded_at.desc(), Document.id.desc())
                ).all()
                return ok([_doc_dict(d) for d in rows], backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def delete_document(self, document_id: str) -> dict:
        # One transaction: the document and its events go together or not at all.
        try:
            with self._db() as db, db.begin():
                row = self._find_doc(db, document_id)
                if row is None:
                    return fail(f"No document {document_id} exists.", NOT_FOUND)
                gone = _doc_dict(row)
                for e in db.scalars(select(CalendarEvent).where(CalendarEvent.document_id == row.id)):
                    db.delete(e)
                db.flush()
                db.delete(row)
            return ok(gone, backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    # -- the calendar ------------------------------------------------------
    def replace_calendar_events(self, document_id: str, events: list[dict]) -> dict:
        try:
            with self._db() as db, db.begin():
                for e in db.scalars(select(CalendarEvent).where(CalendarEvent.document_id == document_id)):
                    db.delete(e)
                db.flush()
                rows = []
                for e in events:
                    fields = {k: e.get(k) for k in CALENDAR_EVENT_FIELDS if k not in ("id", "document_id")}
                    fields["years"] = fields.get("years") or "all"
                    fields["include"] = bool(fields.get("include", True))
                    fields["confirmed"] = bool(fields.get("confirmed"))
                    row = CalendarEvent(document_id=document_id, **fields)
                    db.add(row)
                    rows.append(row)
                db.flush()
                stored = [_cal_dict(r) for r in rows]
            return ok(stored, backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def list_calendar_events(
        self, confirmed_only: bool = True, document_id: str | None = None
    ) -> dict:
        stmt = select(CalendarEvent)
        if confirmed_only:
            stmt = stmt.where(CalendarEvent.confirmed.is_(True))
        if document_id is not None:
            stmt = stmt.where(func.upper(CalendarEvent.document_id) == document_id.strip().upper())
        try:
            with self._db() as db:
                # Undated rows last on every dialect (SQLite sorts NULL first).
                rows = db.scalars(stmt.order_by(CalendarEvent.start_date.is_(None),
                                                CalendarEvent.start_date, CalendarEvent.id)).all()
                return ok([_cal_dict(e) for e in rows], backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    # -- the mess menu -----------------------------------------------------
    MENU_FIELDS = ("mess", "week", "timings", "updated_by", "updated_at")

    def list_menus(self) -> dict:
        try:
            with self._db() as db:
                rows = db.scalars(select(MessMenu).order_by(MessMenu.mess)).all()
                return ok([{k: getattr(r, k) for k in self.MENU_FIELDS} for r in rows], backend=self.name)
        except SQLAlchemyError as e:
            return self._unavailable(e)

    def save_menu(self, mess: str, menu: dict) -> dict:
        fields = {k: menu.get(k) for k in self.MENU_FIELDS if k != "mess"}
        # Two first saves of the same mess both see no row and both insert;
        # the loser retries, finds the row, and updates it.
        for attempt in range(self.ALLOCATE_ATTEMPTS):
            try:
                with self._ALLOCATE, self._db() as db, db.begin():
                    row = db.get(MessMenu, mess)
                    if row is None:
                        row = MessMenu(mess=mess, **fields)
                        db.add(row)
                    else:
                        for k, v in fields.items():
                            setattr(row, k, v)
                    db.flush()
                    stored = {k: getattr(row, k) for k in self.MENU_FIELDS}
                return ok(stored, backend=self.name)
            except IntegrityError:
                self._backoff(attempt)
                continue
            except SQLAlchemyError as e:
                return self._unavailable(e)
        return fail("could not save the menu; please retry", UNAVAILABLE)
