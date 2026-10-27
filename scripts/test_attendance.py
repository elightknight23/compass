"""The attendance projection (README, "The attendance rule"), and the handler that uses it.

    current   = attended / held
    added     = working_days * periods_per_day
    present   = added if condoned else 0
    projected = (attended + present) / (held + added)

The arithmetic is pinned with explicit periods-per-day and threshold, so a
change to the defaults in .env cannot make these pass or fail. Then the
request_od_leave handler is run against a sandboxed backend to check the
two behaviours that are the point of the domain change:

  - OD can RAISE a percentage, and the sentence says so;
  - falling below the threshold FLAGS the request, it does not refuse it.

No LLM, no network.

    python scripts/test_attendance.py
"""

from __future__ import annotations

from _harness import check, finish, sandbox, section

from app import attendance, config, handlers, users
from app.state import new_state

P, T = 6, 0.75  # pinned: periods per day, threshold


def approx(a: float | None, b: float, tol: float = 1e-9) -> bool:
    return a is not None and abs(a - b) < tol


def main() -> int:
    section("the formula")
    p = attendance.project(457, 600, 3, condoned=False, periods_per_day=P, threshold=T)
    check("current is attended / held", approx(p.current_pct, 457 / 600))
    check("non-condoned leave adds periods held but not attended",
          p.added_periods == 18 and p.added_present == 0)
    check("non-condoned: (457) / (618)", approx(p.projected_pct, 457 / 618))
    check("non-condoned leave lowers the percentage", p.projected_pct < p.current_pct)
    check("76.2% -> 73.9% crosses 75%: below_threshold",
          p.below_threshold and not p.already_below,
          f"{attendance.pct(p.current_pct)} -> {attendance.pct(p.projected_pct)}")

    od = attendance.project(457, 600, 3, condoned=True, periods_per_day=P, threshold=T)
    check("condoned leave adds to both sides", od.added_present == od.added_periods == 18)
    check("condoned: (457+18) / (618)", approx(od.projected_pct, 475 / 618))
    check("OD RAISES a percentage below 100%", od.projected_pct > od.current_pct)
    check("  and so does not cross the threshold", not od.below_threshold)

    full = attendance.project(600, 600, 5, condoned=True, periods_per_day=P, threshold=T)
    check("at 100%, OD keeps it at exactly 100%", approx(full.projected_pct, 1.0))

    short = attendance.project(438, 600, 3, condoned=True, periods_per_day=P, threshold=T)
    check("already short + OD: still flagged if the projection stays short",
          short.already_below and short.below_threshold
          and short.projected_pct > short.current_pct)

    lifted = attendance.project(448, 600, 10, condoned=True, periods_per_day=P, threshold=T)
    check("already short, OD lifts them over: NOT flagged — the request isn't the problem",
          lifted.already_below and not lifted.below_threshold,
          f"{attendance.pct(lifted.current_pct)} -> {attendance.pct(lifted.projected_pct)}")

    edge = attendance.project(75, 100, 0, condoned=False, periods_per_day=P, threshold=T)
    check("exactly at the threshold is not below it", not edge.below_threshold)

    zero = attendance.project(0, 0, 2, condoned=False, periods_per_day=P, threshold=T)
    check("held=0: current is None rather than a division error", zero.current_pct is None)
    check("held=0: the projection is still defined", approx(zero.projected_pct, 0.0))
    none = attendance.project(0, 0, 0, condoned=False, periods_per_day=P, threshold=T)
    check("held=0, no leave: nothing to project, nothing flagged",
          none.projected_pct is None and not none.below_threshold)

    bad = attendance.project(700, 600, 1, condoned=False, periods_per_day=P, threshold=T)
    check("attended > held is clamped, never a percentage over 100",
          bad.held == 700 and bad.current_pct == 1.0)
    neg = attendance.project(-5, 600, -2, condoned=False, periods_per_day=P, threshold=T)
    check("negative inputs are clamped to zero", neg.attended == 0 and neg.working_days == 0)

    check("as_dict carries the derived flags",
          {"below_threshold", "already_below"} <= set(p.as_dict()))

    section("formatting")
    check("pct: one decimal place", attendance.pct(457 / 600) == "76.2%")
    check("pct: None renders as a dash", attendance.pct(None) == "—")
    check("rule_pct: a threshold reads as a round number", attendance.rule_pct(0.75) == "75%")
    check("pct: just below the line never rounds UP to it (74.96% -> 74.9%, not 75.0%)",
          attendance.pct(0.7496) == "74.9%", attendance.pct(0.7496))
    check("pct: exactly on the line reads 75.0%", attendance.pct(0.75) == "75.0%")
    check("pct: away from the line, ordinary rounding (0.7494 -> 74.9%, 0.7616 -> 76.2%)",
          attendance.pct(0.7494) == "74.9%" and attendance.pct(0.76166) == "76.2%")
    check("pct: what is displayed never contradicts below_threshold",
          all((attendance.pct(v) < "75.0%") == (v < 0.75)
              for v in (0.7449, 0.7495, 0.74999, 0.75, 0.75001, 0.7551)))

    section("days of margin")
    check("Ravi (457/600) can miss exactly one more day",
          attendance.days_of_margin(457, 600, P, T) == 1)
    check("  and the next day really would cross",
          attendance.project(457, 600, 2, False, P, T).below_threshold)
    check("someone already short has -1", attendance.days_of_margin(438, 600, P, T) == -1)
    check("no periods held has None", attendance.days_of_margin(0, 0, P, T) is None)

    section("condonation is configuration")
    saved = config.OD_IS_CONDONED
    try:
        config.OD_IS_CONDONED = True
        check("OD is condoned when the college condones it", attendance.is_condoned("od"))
        check("medical is never condoned", not attendance.is_condoned("medical"))
        check("casual is never condoned", not attendance.is_condoned("casual"))
        config.OD_IS_CONDONED = False
        check("OD is not condoned when OD_IS_CONDONED=false", not attendance.is_condoned("od"))
    finally:
        config.OD_IS_CONDONED = saved

    section("the handler: request_od_leave (today pinned to Fri 23 Oct 2026)")
    backend = sandbox("json")
    ravi = users.get("U001")

    def apply(**entities):
        st = new_state("leave please", ravi)
        st["entities"] = entities
        return handlers.handle_request_od_leave(st)

    out = apply(leave_kind="casual", start_date="2026-11-02", end_date="2026-11-04")
    d = out["debug"]
    check("casual leave that crosses the line is CREATED, not refused",
          d.get("outcome") == "created", out["reply"])
    check("  and flagged needs_exception", d.get("needs_exception") is True)
    rec = backend.get_record(d["record_id"])["data"]
    check("  the stored record carries the flag and the projection",
          rec["needs_exception"] is True and rec["projection"]["projected_pct"] < 0.75)
    check("  the sentence states the real numbers",
          "76.2%" in out["reply"] and "73.9%" in out["reply"] and "exception" in out["reply"],
          out["reply"])
    check("  and the submission was audited",
          [e["verb"] for e in backend.list_events(d["record_id"])["data"]] == ["submit"])

    out = apply(leave_kind="od", start_date="2026-11-02", end_date="2026-11-04")
    check("OD on the same dates is not flagged",
          out["debug"].get("needs_exception") is False, out["reply"])
    check("  and the sentence says it counts as present and rises",
          "counts as present" in out["reply"] and "76.9%" in out["reply"], out["reply"])

    out = apply(leave_kind="od", start_date="2026-11-16", end_date="2026-11-20")
    check("a week spanning Founders' Day counts four working days, not five",
          out["debug"].get("working_days") == 4, str(out["debug"].get("working_days")))
    check("  and names the holiday it skipped", "Founders' Day" in out["reply"], out["reply"])

    out = apply(leave_kind="medical", start_date="2026-10-24", end_date="2026-10-25")
    check("a weekend-only span creates nothing",
          out["debug"].get("outcome") == "no_working_days")

    out = apply(start_date="2026-11-02")
    check("no leave kind: asks, because condonation depends on it",
          out["debug"].get("outcome") == "needs_leave_kind")
    out = apply(leave_kind="od")
    check("no dates: asks", out["debug"].get("outcome") == "needs_dates")
    out = apply(leave_kind="od", start_date="2026-10-01")
    check("a past start date is refused", out["debug"].get("outcome") == "past_dates")
    out = apply(leave_kind="od", start_date="2026-11-04", end_date="2026-11-02")
    check("an inverted range is refused", out["debug"].get("outcome") == "bad_dates")

    st = new_state("leave please", users.get("U102"))
    st["entities"] = {"leave_kind": "casual", "start_date": "2026-11-02"}
    out = handlers.handle_request_od_leave(st)
    check("staff have no attendance, so no leave request is created",
          out["debug"].get("outcome") == "not_applicable")

    return finish("attendance")


if __name__ == "__main__":
    raise SystemExit(main())
