"""Attendance projection — what a leave request does to a student's percentage.

This replaces the predecessor's notice-period check, and it is the one
genuinely new piece of business logic in the port. It is deliberately the
whole of this module: a pure function of numbers, no I/O, no model.

    current   = attended / held
    added     = working_days * periods_per_day
    present   = added if the leave is condoned else 0
    projected = (attended + present) / (held + added)

Two consequences are the point, not side effects:

1. **Condoned leave can raise a percentage.** An OD day counts as present,
   so for anyone below 100% the projection goes *up*. That is exactly why
   students chase OD instead of casual leave, and a model of the college
   that missed it would be modelling the wrong college.

2. **The system informs; a human decides.** Falling below the threshold
   does not refuse the request. It sets `below_threshold`, the handler
   attaches the numbers to the record, and the advisor has to grant an
   explicit exception to approve it. An agent that silently refuses a
   legitimate request is worse than one that escalates it.

The model never computes any of this (invariant I5). The classifier reports
the dates it heard; `dates.py` turns them into a working-day count; this
module does the division.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from . import config


@dataclass(frozen=True)
class Projection:
    attended: int
    held: int
    working_days: int
    periods_per_day: int
    condoned: bool
    threshold: float

    added_periods: int
    added_present: int
    current_pct: float | None  # None when nothing has been held yet
    projected_pct: float | None

    @property
    def below_threshold(self) -> bool:
        """Whether the request would leave the student short.

        Judged on the projection, not on where they stand today: someone at
        74% applying for OD that lifts them to 76% is not short *because of*
        this request, and flagging it would bury the cases that are.
        """
        return self.projected_pct is not None and self.projected_pct < self.threshold

    @property
    def already_below(self) -> bool:
        return self.current_pct is not None and self.current_pct < self.threshold

    @property
    def delta_pct(self) -> float | None:
        if self.current_pct is None or self.projected_pct is None:
            return None
        return self.projected_pct - self.current_pct

    def as_dict(self) -> dict:
        """JSON-safe snapshot to store on the record and show in the trace."""
        return {
            **asdict(self),
            "below_threshold": self.below_threshold,
            "already_below": self.already_below,
        }


def is_condoned(leave_kind: str | None) -> bool:
    """Which kinds of leave count as present. Only OD, and only if the
    college condones it at all — that is configuration, not a constant."""
    return leave_kind == "od" and config.OD_IS_CONDONED


def project(
    attended: int,
    held: int,
    working_days: int,
    condoned: bool,
    periods_per_day: int | None = None,
    threshold: float | None = None,
) -> Projection:
    """The projection in the README ("The attendance rule"). Never raises for sane inputs.

    Negative inputs are clamped to zero rather than trusted: they can only
    come from bad seed data, and a negative period count would produce a
    percentage that looks plausible and means nothing.
    """
    ppd = config.PERIODS_PER_DAY if periods_per_day is None else periods_per_day
    thr = config.ATTENDANCE_THRESHOLD if threshold is None else threshold

    attended = max(0, int(attended))
    held = max(attended, int(held))  # you cannot attend more than was held
    days = max(0, int(working_days))

    added = days * max(0, ppd)
    present = added if condoned else 0

    current = attended / held if held else None
    total = held + added
    projected = (attended + present) / total if total else None

    return Projection(
        attended=attended,
        held=held,
        working_days=days,
        periods_per_day=ppd,
        condoned=condoned,
        threshold=thr,
        added_periods=added,
        added_present=present,
        current_pct=current,
        projected_pct=projected,
    )


def days_of_margin(
    attended: int,
    held: int,
    periods_per_day: int | None = None,
    threshold: float | None = None,
) -> int | None:
    """Whole days of un-condoned absence before falling below the threshold.

    The largest k with attended / (held + k * periods_per_day) >= threshold,
    found by counting rather than by solving the inequality, so the answer
    is checked by the same projection everything else uses. -1 when the
    student is already below the threshold; None if nothing has been held.
    """
    ppd = config.PERIODS_PER_DAY if periods_per_day is None else periods_per_day
    thr = config.ATTENDANCE_THRESHOLD if threshold is None else threshold
    if held <= 0 or ppd <= 0:
        return None
    if attended / held < thr:
        return -1
    k = 0
    while k < 365 and project(attended, held, k + 1, False, ppd, thr).projected_pct >= thr:
        k += 1
    return k


def pct(value: float | None, threshold: float | None = None) -> str:
    """"76.2%". One decimal place: enough to see a 0.4-point move, few
    enough that nobody mistakes it for more precision than the data has.

    Rounding is never allowed to cross the threshold upward: 74.96% is
    below 75%, and printing it as "75.0%" next to "below the 75%
    requirement" contradicts the decision the code just made. Below the
    threshold the figure is truncated instead, so it reads "74.9%". The
    decision itself always uses the unrounded value.
    """
    if value is None:
        return "—"
    thr = config.ATTENDANCE_THRESHOLD if threshold is None else threshold
    shown = round(value * 100, 1)
    if value < thr and shown >= thr * 100:
        shown = math.floor(value * 1000) / 10
    return f"{shown:.1f}%"


def rule_pct(value: float | None) -> str:
    """"75%" for a threshold. A rule is a round number and reads as one;
    printing it as "75.0%" suggests it was measured."""
    return "—" if value is None else f"{value * 100:g}%"
