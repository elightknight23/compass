"""Restore the demo to its known-good state, on whichever backend is in use.

Run this before every rehearsal and once more immediately before the demo.
A live demo has to be idempotent: the acceptance matrix submits leave and
then approves it, so a second pass over the same store starts from a
different world than the first — and the narration stops matching the screen.

    python scripts/reset_demo.py            # restore RECORD_BACKEND's store
    python scripts/reset_demo.py --check    # report drift, change nothing
    python scripts/reset_demo.py --all      # restore both json and postgres
    python scripts/reset_demo.py --documents  # ALSO remove every uploaded document

The committed truth is data/records.json.seed (plus users, credentials and
attendance). JSON is restored by copying it; Postgres by re-seeding from it.
Attached medical certificates belong to the records, so they go too.

Uploaded documents and their reviewed calendar are kept by default: they
are the College's content, curated by the advisor, not demo state that
drifts. --documents removes them too (registry, files, calendar) and
rebuilds the index from the regulations alone.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

DATA = ROOT / "data"
DOC_FILES = (DATA / "documents.json", DATA / "calendar.json", DATA / "mess_menus.json")
LIVE = DATA / "records.json"
EVENTS = DATA / "events.json"
SEED = DATA / "records.json.seed"


def summarise(rows: list[dict]) -> list[str]:
    return sorted(f"{r['id']} {r['status']}" for r in rows)


def live_rows(backend: str) -> list[dict] | None:
    if backend == "json":
        return json.loads(LIVE.read_text()) if LIVE.exists() else []
    from app.backends.postgres_backend import PostgresBackend

    res = PostgresBackend().list_all_records()
    return res["data"] if res["success"] else None


def restore(backend: str, documents: bool = False) -> bool:
    if backend == "json":
        shutil.copy(SEED, LIVE)
        EVENTS.write_text("[]\n")
        if documents:
            for p in DOC_FILES:
                p.unlink(missing_ok=True)
        print(f"json:     restored {LIVE.relative_to(ROOT)} from seed, events cleared"
              + ("; documents removed" if documents else "; documents kept"))
        return True
    from seed_db import seed  # scripts/ is on the path when run as a script

    try:
        counts = seed(config.DATABASE_URL, keep_documents=not documents)
    except Exception as e:  # noqa: BLE001
        print(f"postgres: FAILED ({type(e).__name__}) — is it up? docker compose up -d")
        return False
    print(f"postgres: re-seeded ({counts['records']} records), events cleared"
          + ("; documents removed" if documents else f"; {counts['documents']} documents kept"))
    return True


def clear_uploads() -> None:
    """The files behind removed documents, and an index without them."""
    shutil.rmtree(config.UPLOADS_DIR, ignore_errors=True)
    from app import rag

    try:
        rag.build_index()
        print("index:    rebuilt from the regulations alone")
    except Exception as e:  # noqa: BLE001
        print(f"index:    not rebuilt ({type(e).__name__}); run scripts/index_docs.py")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report drift, don't restore")
    ap.add_argument("--all", action="store_true", help="restore both backends")
    ap.add_argument("--documents", action="store_true",
                    help="also remove every uploaded document and its calendar")
    args = ap.parse_args()

    current = (config.RECORD_BACKEND or "json").strip().lower()
    targets = ["json", "postgres"] if args.all else [current]
    seed_rows = summarise(json.loads(SEED.read_text()))

    if args.check:
        status = 0
        for b in targets:
            rows = live_rows(b)
            if rows is None:
                print(f"{b}: unreachable")
                status = 1
            elif summarise(rows) != seed_rows:
                print(f"{b}: DRIFTED from seed\n  seed: {seed_rows}\n  live: {summarise(rows)}")
                status = 1
            else:
                print(f"{b}: clean — matches seed ({len(seed_rows)} records)")
        return status

    ok = all([restore(b, documents=args.documents) for b in targets])
    if ok:
        shutil.rmtree(config.CERTIFICATES_DIR, ignore_errors=True)
    if args.documents and ok:
        clear_uploads()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
