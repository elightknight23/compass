"""Date resolution — the model reads the phrase, Python does the arithmetic.

The classifier used to return finished ISO dates. It was wrong in a way
that looked right: asked for "Monday through Friday, four weeks from now"
on a Wednesday, gpt-4o-mini returned a Saturday-to-Wednesday span. Ten
rephrasings all failed identically — it drops the weekday anchor, offsets
from today, and counts five *calendar* days. Simple anchors ("tomorrow",
"next Monday through Wednesday") it gets right; the compound
weekday-plus-week-offset it cannot do.

So the split moved. The model reports what it *heard* — an anchor
weekday, a week offset, a duration — and this module turns that into
dates. Language understanding stays with the LLM; calendar arithmetic
becomes ordinary Python that a unit test can pin down.

That is the same boundary the handlers already draw for authorization:
the model decides what is being asked, code decides the rest.

**Working days skip the college's holidays**, not just weekends. The
calendar is `data/holidays.json`; a college has far more holidays than an
office, and counting Pongal as a working day would put a wrong number into
every attendance projection that spans it. Every function that counts takes
an explicit `holidays` set so tests can pin it, and defaults to the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Literal

from . import clock, config

Weekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]

_INDEX: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class DateSpec:
    """What the model heard, before any arithmetic.

    Every field is optional because most messages carry no dates at all.
    The fields are read in priority order by resolve(): an explicit
    calendar date beats a named weekday, which beats a day offset. The
    model does not reliably fill in only one of them, so the order is a
    real decision and not a formality — see the comment in resolve().
    """

    explicit_start: str | None = None  # ISO, only if the user named a date
    explicit_end: str | None = None
    day_offset: int | None = None  # today=0, tomorrow=1, "in three days"=3
    anchor_weekday: str | None = None  # the weekday leave starts on
    week_offset: int | None = None  # 0=this week, 1=next week, 4="four weeks"
    end_weekday: str | None = None  # the weekday leave ends on
    duration_weekdays: int | None = None  # "three days off" = 3

    def is_empty(self) -> bool:
        return all(
            getattr(self, f) is None
            for f in (
                "explicit_start",
                "day_offset",
                "anchor_weekday",
                "explicit_end",
                "end_weekday",
                "duration_weekdays",
            )
        )


def _parse(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


@lru_cache(maxsize=1)
def college_holidays() -> frozenset[date]:
    """The holiday calendar, read once. A missing or unreadable file means
    "no holidays known" — weekends still count — rather than a crash."""
    try:
        raw = json.loads((config.DATA_DIR / "holidays.json").read_text())
    except (OSError, ValueError):
        return frozenset()
    out = set()
    for entry in raw:
        try:
            out.add(date.fromisoformat(entry["date"]))
        except (KeyError, TypeError, ValueError):
            continue  # one malformed row should not discard the calendar
    return frozenset(out)


def holiday_name(d: date) -> str | None:
    """"Pongal" for 2027-01-15, or None. For sentences, not arithmetic."""
    try:
        raw = json.loads((config.DATA_DIR / "holidays.json").read_text())
    except (OSError, ValueError):
        return None
    iso = d.isoformat()
    return next((e.get("name") for e in raw if e.get("date") == iso), None)


def is_working_day(d: date, holidays: frozenset[date] | None = None) -> bool:
    hols = college_holidays() if holidays is None else holidays
    return d.weekday() < 5 and d not in hols


def working_days(
    start: date, end: date, holidays: frozenset[date] | None = None
) -> int:
    """Working days in [start, end] inclusive — weekdays that are not holidays."""
    hols = college_holidays() if holidays is None else holidays
    return sum(
        1
        for i in range((end - start).days + 1)
        if is_working_day(start + timedelta(days=i), hols)
    )


def holidays_between(
    start: date, end: date, holidays: frozenset[date] | None = None
) -> list[date]:
    """Weekday holidays inside the span — the days a projection skipped."""
    hols = college_holidays() if holidays is None else holidays
    return sorted(d for d in hols if start <= d <= end and d.weekday() < 5)


def _monday_of(d: date) -> date:
    """The Monday that starts d's week. Weeks start Monday throughout."""
    return d - timedelta(days=d.weekday())


def add_weekdays(
    start: date, n: int, holidays: frozenset[date] | None = None
) -> date:
    """The date n working days after start, skipping weekends and holidays.

    add_weekdays(Fri, 1) is the following Monday. n=0 returns start
    unchanged even if start is a weekend — callers decide what a
    weekend start means, this only counts.
    """
    hols = college_holidays() if holidays is None else holidays
    d = start
    remaining = n
    while remaining > 0:
        d += timedelta(days=1)
        if is_working_day(d, hols):
            remaining -= 1
    return d


def resolve(
    spec: DateSpec,
    today: date | None = None,
    holidays: frozenset[date] | None = None,
) -> tuple[date, date] | None:
    """Turn a DateSpec into a (start, end) pair, or None if it has no dates.

    Deliberately total: it never raises and never guesses past a missing
    field. If it cannot find a start it returns None, and the handler
    asks the user for dates rather than inventing them.
    """
    today = today or clock.today()

    # -- start: explicit date, then a named weekday, then a day offset -----
    #
    # The order of the last two is load-bearing and was originally the other
    # way round. Asked for "next Monday through Wednesday", the model has
    # been observed to return day_offset=1 *and* anchor_weekday=monday *and*
    # week_offset=1 — filling in every field it can rather than only the one
    # that was said. Trusting the offset there books tomorrow, and the
    # request sails through as a valid four-day span starting Friday.
    #
    # A named weekday is what the user actually uttered. A day offset is the
    # model having counted, which is precisely the arithmetic this module
    # exists to take away from it. So when both arrive, the utterance wins.
    start = _parse(spec.explicit_start)

    if start is None and spec.anchor_weekday:
        idx = _INDEX.get(spec.anchor_weekday.strip().lower())
        if idx is not None:
            weeks = spec.week_offset if spec.week_offset is not None else 0
            start = _monday_of(today) + timedelta(weeks=weeks, days=idx)
            # "this Monday" when Monday has passed means the next one. An
            # explicit offset is trusted as given — week_offset=0 with a
            # past weekday only happens when the model meant this week.
            if weeks == 0 and start < today:
                start += timedelta(weeks=1)

    if start is None and spec.day_offset is not None:
        start = today + timedelta(days=spec.day_offset)

    if start is None:
        return None

    # -- end: explicit date, then end weekday, then duration ---------------
    end = _parse(spec.explicit_end)

    if end is None and spec.end_weekday:
        idx = _INDEX.get(spec.end_weekday.strip().lower())
        if idx is not None:
            # The named weekday on or after start, inside the same week
            # run — "Monday through Friday" is one span, not eight days.
            end = start + timedelta(days=(idx - start.weekday()) % 7)

    if end is None and spec.duration_weekdays:
        # "three days off" means three days of class missed, so holidays
        # inside the run do not use up the count.
        end = add_weekdays(start, max(0, spec.duration_weekdays - 1), holidays)

    return start, end or start
