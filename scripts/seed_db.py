"""Create the Postgres schema and load the committed seed data into it.

Reads the same files the JSON backend serves — users.json, credentials.json,
attendance.json and records.json.seed — so both backends start from one
known-good world. Idempotent: it drops and recreates every table.

    docker compose up -d
    python scripts/seed_db.py                    # DATABASE_URL from .env
    python scripts/seed_db.py --url sqlite:///x.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from app import config  # noqa: E402
from app.backends import RECORD_FIELDS  # noqa: E402
from app.backends.postgres_backend import init_schema, make_engine  # noqa: E402
from app.models import Attendance, Base, CalendarEvent, Document, MessMenu, Record, User  # noqa: E402


def _rows(engine, model) -> list[dict]:
    from sqlalchemy import inspect, select

    if not inspect(engine).has_table(model.__tablename__):
        return []
    cols = [c.key for c in model.__table__.columns]
    with Session(engine) as db:
        return [{k: getattr(r, k) for k in cols} for r in db.scalars(select(model)).all()]


def seed(url: str, data_dir: Path | None = None, keep_documents: bool = False) -> dict:
    """Drop and recreate every table from the JSON seeds.

    keep_documents carries uploaded documents, their reviewed calendar and
    the saved mess menus across the reset (scripts/reset_demo.py does, by
    default): resetting the demo's records should not make the advisor
    re-upload and re-review the College's calendar, or the warden re-enter
    a menu.
    """
    data = data_dir or config.DATA_DIR
    load = lambda name: json.loads((data / name).read_text())  # noqa: E731

    people = load("users.json")
    creds = {v["user_id"]: (k, v["password"]) for k, v in load("credentials.json").items()}
    engine = make_engine(url)
    kept_docs = _rows(engine, Document) if keep_documents else []
    kept_events = _rows(engine, CalendarEvent) if keep_documents else []
    kept_menus = _rows(engine, MessMenu) if keep_documents else []
    Base.metadata.drop_all(engine)
    init_schema(engine)

    with Session(engine) as db, db.begin():
        for u in people:
            username, password_hash = creds[u["id"]]
            db.add(User(
                id=u["id"], username=username, password_hash=password_hash,
                name=u["name"], role=u["role"], department=u.get("department"),
                branch=u.get("branch"), year=u.get("year"), semester=u.get("semester"),
                section=u.get("section"), mess=u.get("mess"),
                residence=u.get("residence"), context=u.get("context"),
            ))
        db.flush()  # users must exist before rows that reference them
        for a in load("attendance.json"):
            db.add(Attendance(**a))
        for r in load("records.json.seed"):
            db.add(Record(**{k: r.get(k) for k in RECORD_FIELDS}))
        for d in kept_docs:
            db.add(Document(**d))
        db.flush()
        for e in kept_events:
            db.add(CalendarEvent(**e))
        for m in kept_menus:
            db.add(MessMenu(**m))

    engine.dispose()
    return {"users": len(people), "records": len(load("records.json.seed")),
            "documents": len(kept_docs)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=config.DATABASE_URL)
    args = ap.parse_args()
    try:
        counts = seed(args.url)
    except Exception as e:  # noqa: BLE001 — say what failed, not a traceback
        print(f"seeding failed: {type(e).__name__}: {e}")
        print("is the database up?  docker compose up -d")
        return 1
    where = args.url.split("@")[-1]
    print(f"seeded {where}: {counts['users']} users, {counts['records']} records; events, uploaded documents and saved menus cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
