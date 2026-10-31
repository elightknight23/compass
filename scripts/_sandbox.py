"""A throwaway, seeded record store — shared by the offline suites and the
model-backed harnesses alike.

`sandbox()` copies the committed seed files into a temporary directory and
points the whole app at a backend built over that copy. The demo's own
data/records.json is never read or written, so no harness run can leave the
demo in a different world from the one the script describes.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import backends, config, dates, users  # noqa: E402
from app.backends.json_backend import JsonBackend  # noqa: E402

SEED_FILES = ("users.json", "credentials.json", "attendance.json", "holidays.json")


def _seeded_dir(records: list[dict] | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="compass-test-"))
    for name in SEED_FILES:
        shutil.copy(ROOT / "data" / name, tmp / name)
    seed = records if records is not None else json.loads(
        (ROOT / "data" / "records.json.seed").read_text()
    )
    (tmp / "records.json").write_text(json.dumps(seed, indent=2))
    (tmp / "records.json.seed").write_text(json.dumps(seed, indent=2))  # seed_db reads this
    (tmp / "events.json").write_text("[]")
    return tmp


def sandbox(kind: str = "json", records: list[dict] | None = None):
    """Point the app at a fresh, seeded, throwaway backend and return it.

    kind="json"   JsonBackend over a temp copy of the seed files
    kind="sql"    the SQL backend over a temp SQLite file, seeded the same way
    kind="<url>"  the SQL backend over that URL (re-seeded — use a test DB)

    `records` replaces the seeded records, for suites that need a specific
    starting world.
    """
    tmp = _seeded_dir(records)
    if kind == "json":
        backend = JsonBackend(tmp)
    else:
        from seed_db import seed

        from app.backends.postgres_backend import PostgresBackend

        url = f"sqlite:///{tmp / 'compass.db'}" if kind == "sql" else kind
        seed(url, tmp)
        backend = PostgresBackend(url)
        backend._sandbox_dir = tmp  # where its seed files are, for re-seeding in tests
        if kind == "sql":
            backend.name = "sql(sqlite)"

    # Handlers resolve the backend by calling backends.get_backend() at use
    # time, so replacing the module attribute redirects every one of them.
    backends.get_backend = lambda: backend
    users.reload()
    # Uploads land in the same throwaway directory, and the holiday cache
    # is rebuilt from this store rather than whatever the last suite left.
    config.UPLOADS_DIR = tmp / "uploads"
    config.CERTIFICATES_DIR = tmp / "certificates"
    dates.refresh_holidays()
    return backend
