"""Shared plumbing for the offline test suites.

Two guarantees every suite gets by importing this first:

1. **No network.** The model keys are blanked before `app.config` reads the
   environment, so nothing here can reach a model API — and `load_dotenv`
   never overrides a variable that is already set, so .env cannot put them
   back. A handler that would have called the model falls through to its
   designed fallback, which is itself worth testing.

2. **No live data touched.** `sandbox()` (scripts/_sandbox.py) runs each
   suite against a throwaway copy of the seed data.

The clock is pinned to a known Friday unless a suite pins its own.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ["GITHUB_MODELS_TOKEN"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ.setdefault("COMPASS_TODAY", "2026-10-23")  # a Friday

from _sandbox import sandbox  # noqa: E402,F401 — re-exported for the suites

results: list[bool] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append(bool(condition))
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition and detail:
        print(f"       {detail}")


def section(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(0, 66 - len(title)))


def finish(name: str) -> int:
    passed = sum(results)
    print(f"\n{name}: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1
