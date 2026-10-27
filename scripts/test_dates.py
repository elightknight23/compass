"""Unit tests for app/dates — the calendar arithmetic the model gets wrong.

In the predecessor, gpt-4o-mini asked for "Monday through Friday, four
weeks from now" returned Saturday to Wednesday: three working days, not
five, and the record was created anyway because a schema can only promise
a *valid* date, not a correct one. So the model reports the parts it heard
and this module does the arithmetic — and these cases pin that arithmetic.

No LLM, no network. Every case pins an absolute "today", and the resolve
cases pin an empty holiday calendar, so results cannot drift with either.
The holiday cases at the end pin their own.

    python scripts/test_dates.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.dates import (  # noqa: E402
    DateSpec,
    add_weekdays,
    holidays_between,
    is_working_day,
    resolve,
    working_days,
)

NO_HOLIDAYS: frozenset[date] = frozenset()

WED = date(2026, 8, 5)  # a Wednesday
FRI = date(2026, 8, 7)  # a Friday — the weekend-boundary cases
MON = date(2026, 8, 10)  # a Monday

CASES: list[tuple[str, DateSpec, date, tuple[str, str] | None]] = [
    # -- the three demo utterances --------------------------------------
    (
        "#6  Monday through Friday, four weeks from now",
        DateSpec(anchor_weekday="monday", week_offset=4, end_weekday="friday"),
        WED,
        ("2026-08-31", "2026-09-04"),  # Mon .. Fri, 5 working days
    ),
    (
        "#7  next Monday through Wednesday",
        DateSpec(anchor_weekday="monday", week_offset=1, end_weekday="wednesday"),
        WED,
        ("2026-08-10", "2026-08-12"),  # Mon .. Wed, 3 working days
    ),
    (
        "#7b tomorrow (day_offset=1)",
        DateSpec(day_offset=1),
        WED,
        ("2026-08-06", "2026-08-06"),
    ),
    # -- the model over-filling the spec --------------------------------
    # Asked for "next Monday through Wednesday" on a Thursday, gpt-4o-mini
    # returned day_offset=1 AND anchor_weekday=monday AND week_offset=1 AND
    # duration_weekdays=3 — every field it could populate rather than the
    # one that was said. resolve() used to trust the offset and book
    # tomorrow, and the result was a plausible four-day span nobody asked
    # for. The named weekday now wins, because that is the utterance.
    (
        "over-filled: weekday beats a contradictory day_offset",
        DateSpec(day_offset=1, anchor_weekday="monday", week_offset=1,
                 end_weekday="wednesday", duration_weekdays=3),
        WED,
        ("2026-08-10", "2026-08-12"),  # identical to #7, as if unpolluted
    ),
    (
        "over-filled: a bare day_offset still works when no weekday is named",
        DateSpec(day_offset=1, week_offset=1),
        WED,
        ("2026-08-06", "2026-08-06"),  # nothing to prefer, so the offset stands
    ),
    (
        "over-filled: an explicit date still outranks everything",
        DateSpec(explicit_start="2026-09-14", explicit_end="2026-09-16",
                 day_offset=1, anchor_weekday="monday"),
        WED,
        ("2026-09-14", "2026-09-16"),
    ),
    # -- the same phrases from other weekdays: must not drift -----------
    (
        "#6 asked on a Friday",
        DateSpec(anchor_weekday="monday", week_offset=4, end_weekday="friday"),
        FRI,
        ("2026-08-31", "2026-09-04"),  # same week as from Wednesday
    ),
    (
        "#6 asked on a Monday",
        DateSpec(anchor_weekday="monday", week_offset=4, end_weekday="friday"),
        MON,
        ("2026-09-07", "2026-09-11"),  # four weeks on from ITS week
    ),
    (
        "#7 asked on a Friday",
        DateSpec(anchor_weekday="monday", week_offset=1, end_weekday="wednesday"),
        FRI,
        ("2026-08-10", "2026-08-12"),
    ),
    # -- durations -------------------------------------------------------
    (
        "three days starting Tuesday",
        DateSpec(anchor_weekday="tuesday", week_offset=1, duration_weekdays=3),
        WED,
        ("2026-08-11", "2026-08-13"),
    ),
    (
        "duration spanning a weekend (Thu + 3 weekdays -> Mon)",
        DateSpec(anchor_weekday="thursday", week_offset=1, duration_weekdays=3),
        WED,
        ("2026-08-13", "2026-08-17"),  # Thu, Fri, Mon
    ),
    (
        "single day via duration",
        DateSpec(anchor_weekday="monday", week_offset=1, duration_weekdays=1),
        WED,
        ("2026-08-10", "2026-08-10"),
    ),
    # -- this-week rollover ---------------------------------------------
    (
        "'this Monday' after Monday has passed rolls to next",
        DateSpec(anchor_weekday="monday", week_offset=0),
        WED,
        ("2026-08-10", "2026-08-10"),
    ),
    (
        "'this Friday' is still ahead, stays put",
        DateSpec(anchor_weekday="friday", week_offset=0),
        WED,
        ("2026-08-07", "2026-08-07"),
    ),
    # -- explicit dates win ----------------------------------------------
    (
        "explicit calendar dates",
        DateSpec(explicit_start="2026-09-14", explicit_end="2026-09-18"),
        WED,
        ("2026-09-14", "2026-09-18"),
    ),
    (
        "explicit start beats a weekday anchor",
        DateSpec(explicit_start="2026-09-14", anchor_weekday="monday", week_offset=9),
        WED,
        ("2026-09-14", "2026-09-14"),
    ),
    # -- degenerate input -------------------------------------------------
    ("no dates at all", DateSpec(), WED, None),
    ("garbage ISO string", DateSpec(explicit_start="not-a-date"), WED, None),
    ("unknown weekday name", DateSpec(anchor_weekday="funday"), WED, None),
    ("day_offset 0 is today, not falsy-skipped", DateSpec(day_offset=0), WED,
     ("2026-08-05", "2026-08-05")),
]


def main() -> int:
    failures = 0
    checks = 0

    def report(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures, checks
        checks += 1
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
        if not ok and detail:
            print(f"       {detail}")

    for label, spec, today, expected in CASES:
        got = resolve(spec, today, holidays=NO_HOLIDAYS)
        actual = (got[0].isoformat(), got[1].isoformat()) if got else None
        report(label, actual == expected, f"expected {expected}, got {actual}")

    report("add_weekdays: Fri +1 -> Mon",
           add_weekdays(date(2026, 8, 7), 1, NO_HOLIDAYS) == date(2026, 8, 10))
    report("add_weekdays: +0 is identity",
           add_weekdays(date(2026, 8, 5), 0, NO_HOLIDAYS) == date(2026, 8, 5))
    report("add_weekdays: Wed +5 -> next Wed",
           add_weekdays(date(2026, 8, 5), 5, NO_HOLIDAYS) == date(2026, 8, 12))

    # -- the holiday calendar --------------------------------------------
    # Founders' Day, Wed 18 Nov 2026, pinned here rather than read from the
    # file so the cases test the arithmetic, not the calendar's contents.
    founders = frozenset({date(2026, 11, 18)})
    mon, fri = date(2026, 11, 16), date(2026, 11, 20)

    report("working_days: a plain Mon-Fri week is 5",
           working_days(mon, fri, NO_HOLIDAYS) == 5)
    report("working_days: the same week with a Wednesday holiday is 4",
           working_days(mon, fri, founders) == 4)
    report("working_days: a weekend alone is 0",
           working_days(date(2026, 11, 21), date(2026, 11, 22), founders) == 0)
    report("working_days: a holiday alone is 0",
           working_days(date(2026, 11, 18), date(2026, 11, 18), founders) == 0)
    report("is_working_day: the holiday is not one",
           not is_working_day(date(2026, 11, 18), founders))
    report("add_weekdays skips the holiday: Tue +1 -> Thu",
           add_weekdays(date(2026, 11, 17), 1, founders) == date(2026, 11, 19))
    report("holidays_between names the skipped day",
           holidays_between(mon, fri, founders) == [date(2026, 11, 18)])
    report("holidays_between ignores a holiday that falls on a weekend",
           holidays_between(date(2027, 8, 9), date(2027, 8, 20),
                            frozenset({date(2027, 8, 15)})) == [])

    spec = DateSpec(anchor_weekday="tuesday", week_offset=0, duration_weekdays=3)
    got = resolve(spec, date(2026, 11, 16), holidays=founders)
    report('"three days starting Tuesday" across the holiday ends Friday',
           got == (date(2026, 11, 17), date(2026, 11, 20)), f"got {got}")
    got = resolve(spec, date(2026, 11, 16), holidays=NO_HOLIDAYS)
    report("  and without the holiday it ends Thursday",
           got == (date(2026, 11, 17), date(2026, 11, 19)), f"got {got}")

    # -- what the classifier hands over ------------------------------------
    # Models often say "" or "null" for a part they mean to leave out. That
    # must arrive here as None, not as a string the date parser chokes on
    # or a Literal that fails validation and degrades the turn to clarify.
    from app.graph import Classification

    c = Classification.model_validate({
        "intent": "request_od_leave", "confidence": 0.9, "reasoning": "r",
        "anchor_weekday": "", "explicit_start_date": "null", "leave_kind": "None",
        "week_offset": None, "reason": "a family function",
    })
    report('classifier: "" / "null" / "None" in optional parts become None',
           (c.anchor_weekday, c.explicit_start_date, c.leave_kind) == (None, None, None))
    report("classifier: real values are untouched", c.reason == "a family function")
    try:
        Classification.model_validate({"intent": "", "confidence": 0.9, "reasoning": "r"})
        report("classifier: a blank REQUIRED field still fails loudly", False)
    except ValueError:
        report("classifier: a blank REQUIRED field still fails loudly", True)

    print(f"\n{checks - failures}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
