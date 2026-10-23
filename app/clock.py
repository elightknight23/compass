"""One definition of "today" — and one way to pin it.

Every date Compass reasons about traces back to this function: the date
stamped into the classifier's system prompt, the weekday arithmetic in
`dates.resolve`, the past-date guard on leave, and the `created_at` a
backend writes. Nothing else calls `date.today()`, which makes "today" a
value rather than an ambient fact — and so something a test can set.

That matters for a concrete reason. "I need tomorrow off", sent on a
Saturday, is a Sunday: zero working days, a different outcome from the same
sentence on a Tuesday. If the clock is an untracked input, a test suite
can only ever tell you about the day you happened to run it.

    COMPASS_TODAY=2026-11-16 python scripts/run_acceptance.py

**This is a test affordance and it announces itself.** A pinned clock
prints a warning to stderr the first time it is read, because an agent
quietly wrong about the date is precisely the failure the deterministic
date layer exists to prevent. A bad value falls back to the real date
rather than raising — being unable to parse an override is no reason to
take the server down.
"""

from __future__ import annotations

import os
import sys
from datetime import date

ENV_VAR = "COMPASS_TODAY"

# What we last warned about. Keyed by value so that changing the pin
# mid-process warns again, rather than one warning covering every date.
_warned: str | None = None


def _warn_once(key: str, message: str) -> None:
    global _warned
    if _warned == key:
        return
    _warned = key
    # stderr, never stdout: harnesses capture stdout as evidence, and a
    # warning interleaved into a transcript would corrupt it.
    print(f"[compass-clock] {message}", file=sys.stderr)


def today() -> date:
    """The current date, or the pinned one if COMPASS_TODAY is set."""
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        return date.today()

    try:
        pinned = date.fromisoformat(raw)
    except ValueError:
        _warn_once(
            f"bad:{raw}",
            f"{ENV_VAR}={raw!r} is not an ISO date (YYYY-MM-DD). "
            "Ignoring it and using the real date.",
        )
        return date.today()

    _warn_once(
        f"ok:{raw}",
        f"{ENV_VAR}={raw} — Compass believes today is "
        f"{pinned:%A %-d %B %Y}. Unset it before a real run.",
    )
    return pinned


def is_pinned() -> bool:
    """Whether the clock is currently overridden. For anything that wants to
    say so out loud — a test header, a demo banner."""
    return bool(os.getenv(ENV_VAR, "").strip())
