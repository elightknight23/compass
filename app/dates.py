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

**Working days skip the college's holidays**, not just weekends. A college
has far more holidays than an office, and counting Pongal as a working day
would put a wrong number into every attendance projection that spans it.
Every function that counts takes an explicit `holidays` set so tests can
pin it, and defaults to `college_holidays()`.

Where the holidays come from, in order:

1. **Confirmed holidays from an uploaded academic calendar.** Parsed out of
   the document by app/documents.py — by regex, never by the model — and
   confirmed row by row by the advisor. A holiday may apply to some years
   only (a first-year-only break), so the set is asked for per year.
2. **`data/holidays.json`**, the placeholder seed, when no calendar has
   been confirmed. Replies say which of the two they used.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Literal, get_args

from . import clock, config

Weekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]

_INDEX: dict[str, int] = {d: i for i, d in enumerate(get_args(Weekday))}
Month = Literal[
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
]


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
    # The user described days already gone ("I was sick on Monday"). Only
    # changes one thing: a weekday earlier this week is not rolled forward.
    past: bool = False

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
def _holiday_table() -> tuple[str, tuple[tuple[date, str, frozenset[int] | None], ...]]:
    """(source, ((day, name, years or None for all), ...)), cached.

    Cleared by refresh_holidays() whenever a calendar is confirmed or
    deleted. A store that cannot be read falls back to the seed file —
    and says so on stderr — rather than counting every day as working.
    """
    from . import backends

    res = backends.get_backend().list_calendar_events(confirmed_only=True)
    if not res["success"]:
        print(f"[compass-dates] calendar unavailable ({res['error']}); using data/holidays.json",
              file=sys.stderr)
    confirmed = (res["data"] or []) if res["success"] else []
    rows = []
    for e in confirmed:
        if e.get("kind") != "holiday" or not e.get("include", True):
            continue
        start, end = _parse(e.get("start_date")), _parse(e.get("end_date") or e.get("start_date"))
        if start is None or end is None or end < start or (end - start).days > 120:
            continue
        years = _years(e.get("years"))
        d = start
        while d <= end:
            rows.append((d, e.get("title") or "Holiday", years))
            d += timedelta(days=1)
    # Any confirmed calendar replaces the seed outright — even one with no
    # holidays in it. Mixing the placeholder file into a real calendar
    # would invent days off the college never announced.
    if confirmed:
        return "calendar", tuple(rows)

    seed = _seed_rows()
    if seed is None:
        return "none", ()
    return "seed", tuple((d, name, None) for d, name in seed)


def _seed_rows() -> list[tuple[date, str]] | None:
    """data/holidays.json as (day, name) rows; None if it can't be read."""
    try:
        raw = json.loads((config.DATA_DIR / "holidays.json").read_text())
    except (OSError, ValueError):
        return None
    rows = []
    for entry in raw:
        try:
            rows.append((date.fromisoformat(entry["date"]), entry.get("name") or "Holiday"))
        except (KeyError, TypeError, ValueError):
            continue  # one malformed row should not discard the calendar
    return rows


def _years(value) -> frozenset[int] | None:
    """"all" / None -> None (every year); "1,2" -> {1, 2}."""
    if value in (None, "", "all"):
        return None
    try:
        got = frozenset(int(x) for x in str(value).split(",") if x.strip())
    except ValueError:
        return None
    return got or None


def event_applies(event: dict, year: int | None) -> bool:
    """Whether a calendar event's `years` ("all", "1,2") covers this year."""
    return _applies(_years(event.get("years")), year)


def seed_holidays() -> list[dict]:
    """The placeholder file's holidays, shaped like calendar events."""
    return [{"id": None, "document_id": None, "start_date": d.isoformat(),
             "end_date": d.isoformat(), "title": name,
             "kind": "holiday", "years": "all", "include": True, "confirmed": True}
            for d, name in _seed_rows() or []]


def _applies(years: frozenset[int] | None, year: int | None) -> bool:
    # A holiday for some years only never counts for an unknown year:
    # treating a first-year break as everyone's would shorten every
    # projection that crosses it.
    return years is None or (year is not None and year in years)


def college_holidays(year: int | None = None) -> frozenset[date]:
    """Every holiday that applies to a student in `year` (or, for None, to
    every year). Weekends are handled separately by is_working_day."""
    _, rows = _holiday_table()
    return frozenset(d for d, _, ys in rows if _applies(ys, year))


def holiday_source() -> str:
    """'calendar' (an uploaded, confirmed calendar), 'seed' or 'none'."""
    return _holiday_table()[0]


def refresh_holidays() -> None:
    _holiday_table.cache_clear()


def holiday_name(d: date, year: int | None = None) -> str | None:
    """"Pongal" for 2027-01-15, or None. For sentences, not arithmetic."""
    _, rows = _holiday_table()
    return next((name for day, name, ys in rows if day == d and _applies(ys, year)), None)


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
            # "this Monday" when Monday has passed means the next one —
            # unless the user is talking about days already gone ("I was
            # sick on Monday"). An explicit offset is trusted as given.
            if weeks == 0 and start < today and not spec.past:
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
