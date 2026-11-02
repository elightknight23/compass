"""SQLAlchemy models — the Postgres schema.

Six tables, and what each one is for:

  users          the directory, with the scrypt hash beside it. The API never
                 selects password_hash into anything it serialises; the
                 backend's list_users() leaves it out by construction.
  attendance     one row per student: periods attended and held, and as of
                 when. Seeded, not synced — there is no ERP integration, and
                 a CSV import is the honest interim.
  records        both record types in one table. Type-specific columns are
                 nullable; `type` says which ones mean something. The
                 attendance projection is stored as JSON on the request, as
                 a snapshot: it records what the advisor was shown when
                 they decided, which a later recomputation could not.
  record_events  the audit trail — who did what to which record, from which
                 status to which. Append-only by convention.
  documents      uploaded files (calendars, syllabi, regulations): who
                 uploaded what, which branch and semester it is for, and
                 whether it made it into the index. The file itself lives
                 on disk under data/uploads/, the vectors in ChromaDB.
  calendar_events  dated events parsed from an uploaded calendar. Parsed by
                 Python, confirmed by a person; only confirmed holidays
                 change the working-day arithmetic.

Kept dialect-neutral (JSON rather than JSONB, String ids) so the same
backend can run against SQLite in the offline test suite and against
Postgres for real.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), index=True)
    department: Mapped[str | None] = mapped_column(String(128))
    branch: Mapped[str | None] = mapped_column(String(16))  # "CSE" — filters the syllabus
    year: Mapped[int | None] = mapped_column(Integer)
    semester: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(32))
    mess: Mapped[str | None] = mapped_column(String(16))  # a hosteller's home mess, "IFC A"
    residence: Mapped[str | None] = mapped_column(String(32))
    context: Mapped[str | None] = mapped_column(Text)


class Attendance(Base):
    __tablename__ = "attendance"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    attended: Mapped[int] = mapped_column(Integer)
    held: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[str | None] = mapped_column(String(10))  # ISO date


class Record(Base):
    __tablename__ = "records"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[str] = mapped_column(String(10))  # ISO date, from app.clock
    notes: Mapped[str | None] = mapped_column(Text)

    # facility_complaint
    category: Mapped[str | None] = mapped_column(String(16))
    priority: Mapped[str | None] = mapped_column(String(8))

    # od_request
    leave_kind: Mapped[str | None] = mapped_column(String(24))
    start_date: Mapped[str | None] = mapped_column(String(10))
    end_date: Mapped[str | None] = mapped_column(String(10))
    working_days: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    projection: Mapped[dict | None] = mapped_column(JSON)
    needs_exception: Mapped[bool | None] = mapped_column(Boolean)
    certificate: Mapped[dict | None] = mapped_column(JSON)

    # facility_complaint, when mirrored to the external tracker
    tracker: Mapped[dict | None] = mapped_column(JSON)

    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[str | None] = mapped_column(String(10))


class RecordEvent(Base):
    __tablename__ = "record_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    verb: Mapped[str] = mapped_column(String(16))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    pages: Mapped[int | None] = mapped_column(Integer)
    doc_type: Mapped[str] = mapped_column(String(32), index=True)
    branch: Mapped[str] = mapped_column(String(16))
    semester: Mapped[int | None] = mapped_column(Integer)
    academic_year: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    chunks: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[str] = mapped_column(String(32))  # ISO timestamp


class MessMenu(Base):
    """One mess's saved week. Absent until the warden saves one; the sample
    menu is served for it until then (app/mess.py)."""

    __tablename__ = "mess_menus"

    mess: Mapped[str] = mapped_column(String(16), primary_key=True)  # "IFC A"
    week: Mapped[dict] = mapped_column(JSON)
    timings: Mapped[dict | None] = mapped_column(JSON)
    updated_by: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[str | None] = mapped_column(String(32))


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    start_date: Mapped[str | None] = mapped_column(String(10), index=True)  # ISO date
    end_date: Mapped[str | None] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    years: Mapped[str] = mapped_column(String(32), default="all")
    source_text: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    flags: Mapped[list | None] = mapped_column(JSON)
    include: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
