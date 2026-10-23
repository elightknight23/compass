"""Shared plumbing for the offline test suites.

Two guarantees every suite gets by importing this first:

1. **No network.** The model keys are blanked before `app.config` reads the
   environment, so nothing here can reach a model API — and `load_dotenv`
   never overrides a variable that is already set, so .env cannot put them
   back. A handler that would have called the model falls through to its
   designed fallback, which is itself worth testing.

2. **No live data touched.** `sandbox()` copies the committed seed files into
   a temporary directory and points the whole app at a backend built over
   that copy. The demo's own data/records.json is never read or written, so
   running the suite cannot leave the demo in a different world.

The clock is pinned to a known Friday unless a suite pins its own.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ["GITHUB_MODELS_TOKEN"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ.setdefault("COMPASS_TODAY", "2026-10-23")  # a Friday

from app import backends, users  # noqa: E402
from app.backends.json_backend import JsonBackend  # noqa: E402

SEED_FILES = ("users.json", "credentials.json", "attendance.json", "holidays.json")

results: list[bool] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append(bool(condition))
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition and detail:
        print(f"       {detail}")


def section(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(0, 66 - len(title)))


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
        if kind == "sql":
            backend.name = "sql(sqlite)"

    # Handlers resolve the backend by calling backends.get_backend() at use
    # time, so replacing the module attribute redirects every one of them.
    backends.get_backend = lambda: backend
    users.reload()
    return backend


def finish(name: str) -> int:
    passed = sum(results)
    print(f"\n{name}: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1
