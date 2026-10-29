"""calendar_question and document_question, with the classifier's part
played by hand — no model, a pinned clock (Fri 23 Oct 2026).

The classifier only reports what it heard ("holidays", "november", "the
next one", "Friday"); everything this suite checks is what Python does with
that: which span it means, which events fall in it, which year's holidays
apply, what is said when nothing matches, and that the placeholder
calendar is never passed off as the College's.

document_question is checked up to the model call: which filter it builds
for whom, and that a filter matching nothing becomes an "isn't uploaded"
answer instead of another branch's books.

    python scripts/test_calendar.py
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

from _harness import check, finish, sandbox, section

import chromadb

from app import handlers, library, rag
from app.state import new_state

RAVI = {"id": "U001", "name": "Ravi Kumar", "role": "student", "year": 3, "branch": "CSE", "semester": 5}
FRESHER = {"id": "U009", "name": "A Fresher", "role": "student", "year": 1, "branch": "CSE", "semester": 1}
ADVISOR = {"id": "U102", "name": "Dr. Lakshmi Narayanan", "role": "faculty_advisor"}
TODAY = date(2026, 10, 23)  # _harness pins COMPASS_TODAY to this Friday


def ask(user: dict, **entities) -> dict:
    state = new_state("(heard)", user)
    state["entities"] = {k: v for k, v in entities.items() if v is not None}
    return handlers.handle_calendar_question(state)


def reply(out: dict) -> str:
    return out["reply"]


def calendar_doc() -> str:
    """A confirmed calendar in the sandbox store, as the advisor leaves it."""
    b = library.backends.get_backend()
    doc = b.create_document(
        title="Academic Calendar 2026-27", filename="cal.pdf", content_type="application/pdf",
        sha256="ca" * 32, size=1, doc_type="academic_calendar", branch="ALL",
        academic_year="2026-27", status="indexed", uploaded_by="U102",
        uploaded_at="2026-10-01T00:00:00+00:00",
    )["data"]
    rows = [
        ("2026-10-21", "2026-10-23", "Ayudha Pooja / Vijayadasami holidays", "holiday", "all"),
        ("2026-11-09", "2026-11-09", "Deepavali holiday", "holiday", "all"),
        ("2026-11-12", "2026-11-12", "Freshers' induction break", "holiday", "1"),
        ("2026-11-18", "2026-11-18", "Founders' Day holiday", "holiday", "all"),
        ("2026-11-23", "2026-11-28", "Continuous Assessment Test II", "exam", "all"),
        ("2026-12-24", "2026-12-24", "Last working day for odd semester", "semester_end", "all"),
        ("2026-12-25", "2026-12-25", "Christmas", "holiday", "all"),
        ("2027-01-14", "2027-01-16", "Pongal holidays", "holiday", "all"),
    ]
    b.replace_calendar_events(doc["id"], [
        dict(start_date=s, end_date=e, title=t, kind=k, years=y, include=True, confirmed=True)
        for s, e, t, k, y in rows
    ] + [dict(start_date="2026-12-05", end_date="2026-12-05", title="Saturday working",
              kind="other", years="all", include=False, confirmed=False)])
    library.dates.refresh_holidays()
    return doc["id"]


def main() -> int:
    sandbox("json")

    section("before any calendar is confirmed")
    out = ask(RAVI, calendar_kind="holiday", next_only=True)
    check("the placeholder list answers, and says it is the placeholder",
          "Founders' Day" in reply(out) and "placeholder" in reply(out), reply(out))
    check("  and the trace names its source", out["debug"]["calendar"]["source"] == "seed")

    doc_id = calendar_doc()

    section("spans, read in Python from what was heard")
    lo, hi, mode = handlers.calendar_range({"month": "november"}, TODAY)
    check("'November' asked in October is this November",
          (lo, hi, mode) == (date(2026, 11, 1), date(2026, 11, 30), "month"))
    lo, hi, _ = handlers.calendar_range({"month": "july"}, TODAY)
    check("'July' asked in October is NEXT July", (lo, hi) == (date(2027, 7, 1), date(2027, 7, 31)))
    lo, hi, mode = handlers.calendar_range({"week_offset": 1}, TODAY)
    check("'next week' with no weekday is Monday to Sunday of next week",
          (lo, hi, mode) == (date(2026, 10, 26), date(2026, 11, 1), "week"))
    _, _, mode = handlers.calendar_range({"start_date": "2026-11-09", "end_date": "2026-11-09"}, TODAY)
    check("one resolved date is a single-day question", mode == "day")
    lo, hi, mode = handlers.calendar_range({}, TODAY)
    check("no span at all means the coming weeks", (lo, mode) == (TODAY, "upcoming") and (hi - lo).days == 60)

    section("answers")
    out = ask(RAVI, calendar_kind="holiday", next_only=True)
    check("the next holiday, on a day inside one, is that holiday ('Today')",
          "Today" in reply(out) and "Ayudha Pooja" in reply(out), reply(out))
    check("  cited to the confirmed calendar, not the placeholder",
          "Academic Calendar 2026-27" in reply(out) and doc_id in reply(out)
          and "placeholder" not in reply(out))
    out = ask(RAVI, calendar_kind="holiday", month="november")
    r = reply(out)
    check("November's holidays for a third year: Deepavali and Founders' Day",
          "Deepavali" in r and "Founders' Day" in r, r)
    check("  not the first-years-only break", "Freshers" not in r)
    check("  and not the CAT, which is an exam", "Assessment" not in r)
    out = ask(FRESHER, calendar_kind="holiday", month="november")
    check("a first year's November does include their break", "Freshers" in reply(out))
    out = ask(RAVI, calendar_kind="exam", month="november")
    check("exams in November: CAT II, as a range",
          "Continuous Assessment Test II" in reply(out) and "Mon 23 Nov – Sat 28 Nov" in reply(out), reply(out))
    out = ask(RAVI, calendar_kind="exam", week_offset=1)
    check("no exams next week is said, with the week it searched",
          out["debug"]["outcome"] == "none_found" and "week of Mon 26 Oct" in reply(out), reply(out))
    out = ask(RAVI, calendar_kind="semester", next_only=True)
    check("the next semester date: the last working day", "Last working day" in reply(out))
    out = ask(RAVI, calendar_kind="any")
    check("'what's coming up' lists events in the next 60 days, in date order",
          reply(out).index("Deepavali") < reply(out).index("Founders"), reply(out))
    check("unticked rows never appear (Saturday working)", "Saturday working" not in reply(out))

    section("is there college on ...?")
    out = ask(RAVI, calendar_kind="any", start_date="2026-11-09", end_date="2026-11-09")
    check("a confirmed holiday: 'is a holiday — Deepavali holiday'",
          "is a holiday" in reply(out) and "Deepavali" in reply(out), reply(out))
    out = ask(RAVI, calendar_kind="any", start_date="2026-11-12", end_date="2026-11-12")
    check("a first-years-only holiday is a working day for a third year", "is a working day" in reply(out))
    out = ask(FRESHER, calendar_kind="any", start_date="2026-11-12", end_date="2026-11-12")
    check("  and a holiday for a first year", "is a holiday" in reply(out), reply(out))
    out = ask(RAVI, calendar_kind="any", start_date="2026-11-24", end_date="2026-11-24")
    check("a working day with an exam on it says both",
          "is a working day" in reply(out) and "Assessment" in reply(out), reply(out))
    out = ask(RAVI, calendar_kind="any", start_date="2026-10-31", end_date="2026-10-31")
    check("a Saturday is a weekend, not a holiday", "no regular classes" in reply(out))
    out = ask(ADVISOR, calendar_kind="holiday", month="november")
    check("staff see holidays for every year only (not the first-years' break)",
          "Freshers" not in reply(out) and "Deepavali" in reply(out))
    check("no signed-in user, no answer",
          "sign in" in handlers.handle_calendar_question(new_state("x", None))["reply"])

    section("document_question: the filter, then absence")
    tmp = Path(tempfile.mkdtemp(prefix="compass-cal-"))
    store = chromadb.PersistentClient(path=str(tmp / "chroma"))
    saved = rag.chroma
    rag.chroma = lambda: store
    try:
        rag.build_index()
        state = new_state("textbook for computer networks?", RAVI)
        state["entities"] = {"doc_scope": "syllabus"}
        out = handlers.handle_document_question(state)
        check("no syllabus uploaded for CSE: says so, names the branch, and the model is never asked",
              out["debug"]["outcome"] == "no_documents" and "No syllabus for CSE" in out["reply"]
              and "provider" not in out["debug"], out["reply"])
        check("  and the trace shows the filter that was applied",
              out["debug"]["filter"] == {"scope": "syllabus", "doc_type": "syllabus", "branch": "CSE"})
        state["entities"] = {"doc_scope": "syllabus", "my_courses": True}
        out = handlers.handle_document_question(state)
        check("'my subjects' adds the student's semester to the absence sentence",
              "No syllabus for CSE semester 5" in out["reply"], out["reply"])
        state = new_state("what is the minimum attendance?", RAVI)
        state["entities"] = {"doc_scope": "regulations"}
        out = handlers.handle_document_question(state)
        check("a regulations question with no model configured fails soft, after retrieval",
              out["debug"]["outcome"] == "model_unavailable"
              and out["debug"]["filter"]["doc_type"] == "regulation", str(out["debug"])[:200])
    finally:
        rag.chroma = saved
        shutil.rmtree(tmp, ignore_errors=True)

    return finish("calendar / document questions")


if __name__ == "__main__":
    raise SystemExit(main())
