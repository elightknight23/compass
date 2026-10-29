"""Intent handlers — where the agent actually does things.

Three rules hold across every handler in this file:

  Authority  The actor is state["user"], resolved by FastAPI from a signed
             session cookie. Names extracted from the message only ever
             select a *target* record. Permission checks are plain Python
             `if` statements at the point of the write — a model cannot be
             talked out of an if statement.

  Honesty    Backends return {success, ...} envelopes rather than raising.
             A failed write produces "I couldn't reach the records system",
             never a fabricated record id, never a stack trace. "You may
             not", "that record is already resolved" and "no such record"
             are three different sentences.

  Arithmetic Nothing here asks the model for a number. Dates come from
             dates.resolve, working days from dates.working_days, and the
             attendance projection from attendance.project. The model's
             job ended at classification.
"""

from __future__ import annotations

from datetime import date

from . import attendance, auth, backends, clock, dates, llm, rag, users
from .backends import COMPLAINT, OD_REQUEST
from .state import AgentState

CAPABILITIES = (
    "I can answer questions about college regulations, file hostel, lab, "
    "library and transport complaints, apply for OD or leave (and show what "
    "it does to your attendance), check the status of your requests, and "
    "withdraw anything of yours that's still open."
)


def _out(reply: str, **debug) -> dict:
    """A node result. `debug` merges into the running trace (see state.py)."""
    return {"reply": reply, "debug": debug}


def _no_user() -> dict:
    return _out(
        "I couldn't tell who you are, so I can't act on any records. "
        "Please sign in and try again.",
        authz="no identity on request",
    )


def _trace(envelope: dict) -> dict:
    """The debug extras carried on an envelope, minus the envelope's own
    plumbing. Lets one result populate both a reply and the reasoning panel."""
    return {
        k: v
        for k, v in envelope.items()
        if k not in {"success", "data", "kind"} and v is not None
    }


def _day(d: date) -> str:
    """"Mon 23 Nov". An ISO date in a sentence reads like a database row."""
    return f"{d:%a} {d.day} {d:%b}"


def _span(start: date, end: date) -> str:
    if start == end:
        return _day(start)
    return f"{_day(start)} – {_day(end)}"


_NUMBER_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve"
).split()


def _days(n: int, capital: bool = False) -> str:
    """"three days", "one day". Spelled out up to twelve, as prose would."""
    word = _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)
    if capital:
        word = word[:1].upper() + word[1:]
    return f"{word} day{'' if n == 1 else 's'}"


# ---------------------------------------------------------------------------
# Record vocabulary — facts about record TYPES, true whoever is asking
# ---------------------------------------------------------------------------
# The one status from which each type can still change. Everything else is
# terminal, and an action on a terminal record is refused with its own
# sentence rather than a generic "not allowed".
OPEN_STATUS = {COMPLAINT: "open", OD_REQUEST: "submitted"}

# What withdrawing your OWN record does. A complaint you no longer need is
# cancelled; a leave request you no longer need is withdrawn — the word the
# college's own forms use, and distinct from staff cancelling it.
OWNER_CANCEL_STATUS = {COMPLAINT: "cancelled", OD_REQUEST: "withdrawn"}

# What a staff verb writes.
ACTION_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "resolve": "resolved",
    "cancel": "cancelled",
}

# Which record types each verb makes sense on. Approving a complaint is not
# a smaller mistake than approving nothing — it writes a status that means
# something else, and the record looks actioned afterwards.
ACTION_APPLIES_TO = {
    "approve": {OD_REQUEST},
    "reject": {OD_REQUEST},
    "resolve": {COMPLAINT},
    "cancel": {OD_REQUEST, COMPLAINT},
}

# What an owner may change about their own record. Deliberately excludes
# `status` — closing a record is a staff action — and the dates of a leave
# request, because new dates mean a new attendance projection, which means
# a new request.
AMENDABLE = {COMPLAINT: ("priority", "category"), OD_REQUEST: ("reason",)}

TYPE_NAMES = {COMPLAINT: "a facility complaint", OD_REQUEST: "a leave request"}
TYPE_PLURAL = {COMPLAINT: "facility complaints", OD_REQUEST: "OD and leave requests"}
LEAVE_NAMES = {"od": "OD", "medical": "medical leave", "casual": "casual leave"}

# Which staff role owns each record type, and what to call them out loud.
OWNING_ROLE = {COMPLAINT: "hostel_warden", OD_REQUEST: "faculty_advisor"}
ROLE_LABEL = {
    "student": "a student account",
    "hostel_warden": "the hostel warden",
    "faculty_advisor": "the faculty advisor",
}


def _is_open(record: dict) -> bool:
    return record.get("status") == OPEN_STATUS.get(record.get("type"))


def _who_can(record_type: str | None) -> str:
    """"the faculty advisor (Dr. Lakshmi Narayanan)" — who to go to.

    Read from the directory rather than hardcoded, so adding a second
    advisor does not leave a refusal naming only the first.
    """
    role = OWNING_ROLE.get(record_type)
    label = ROLE_LABEL.get(role, "staff")
    people = [u["name"] for u in users.all_users() if u.get("role") == role]
    return f"{label} ({', '.join(people)})" if people else label


def _recency_key(record: dict) -> tuple:
    """Sort key for "the one they just made".

    `created_at` is a date, not a timestamp, so several records filed in one
    session tie on it — which is exactly the case this has to get right. The
    id breaks the tie, numerically: string order puts CMP-10 before CMP-9.
    """
    suffix = str(record.get("id", "")).rsplit("-", 1)[-1]
    return (record.get("created_at") or "", int(suffix) if suffix.isdigit() else -1)


# ---------------------------------------------------------------------------
# document_question — what the College's documents say
# ---------------------------------------------------------------------------
def handle_document_question(state: AgentState) -> dict:
    status = rag.index_status()
    if status["state"] == "missing":
        return _out(
            "The document index hasn't been built yet, so I can't look "
            "that up. (Staff: run scripts/index_docs.py.)",
            index=status, outcome="index_missing",
        )
    e = state["entities"]
    # Whose documents to search is decided here, in Python, from what the
    # classifier heard and who is signed in — never by the model.
    where, described = rag.build_filter(
        state["user"],
        scope=e.get("doc_scope"),
        branch=e.get("branch"),
        semester=e.get("semester"),
        my_courses=bool(e.get("my_courses")),
    )
    try:
        result = rag.answer_from_kb(state["current_message"], asker=state["user"], where=where)
    except llm.LLMUnavailable as err:
        return _out(
            "I couldn't reach the language model just now, so I can't answer "
            "from the College's documents. Please try again in a minute.",
            error=str(err), outcome="model_unavailable", filter=described,
        )
    if result["answer"] is None:
        # A filtered search that matched nothing is an answer (I6): say what
        # is missing rather than widening the search to someone else's books.
        missing = rag.describe_absence(described) if where else "I couldn't find anything on that"
        return _out(
            f"{missing}, so I can't answer that from it. Your faculty advisor "
            "can upload it on the Documents page.",
            filter=described, index=status, outcome="no_documents",
        )
    return _out(
        result["answer"],
        sources=result["sources"],
        provider=result["provider"],
        index=status,
        filter=described,
        retrieved=[
            {
                "source": c["metadata"]["source"],
                "section": c["metadata"].get("header_path"),
                "doc_id": c["metadata"].get("doc_id"),
                "distance": round(c["distance"], 4),
            }
            for c in result["chunks"]
        ],
        outcome="answered",
    )


# ---------------------------------------------------------------------------
# calendar_question — when things happen, answered from confirmed dates
# ---------------------------------------------------------------------------
CALENDAR_KINDS = {
    "holiday": {"holiday"},
    "exam": {"exam"},
    "semester": {"semester_start", "semester_end"},
}
KIND_WORDS = {"holiday": "holidays", "exam": "exams", "semester": "semester dates"}
MONTH_INDEX = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
UPCOMING_DAYS = 60
MAX_LISTED = 10


def calendar_range(entities: dict, today: date) -> tuple[date, date | None, str]:
    """(from, to or None for open-ended, mode) — the span a question means.

    Every case is Python reading the parts the classifier heard: a resolved
    date range, a month (its next occurrence), a bare week offset, "the
    next one", or by default the coming weeks.
    """
    from calendar import monthrange
    from datetime import timedelta

    if entities.get("start_date"):
        lo = date.fromisoformat(entities["start_date"])
        hi = date.fromisoformat(entities.get("end_date") or entities["start_date"])
        return lo, hi, "day" if lo == hi else "range"
    if entities.get("month") in MONTH_INDEX:
        m = MONTH_INDEX[entities["month"]]
        y = today.year if m >= today.month else today.year + 1
        return date(y, m, 1), date(y, m, monthrange(y, m)[1]), "month"
    if entities.get("week_offset") is not None:
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=int(entities["week_offset"]))
        return monday, monday + timedelta(days=6), "week"
    if entities.get("next_only"):
        return today, None, "next"
    return today, today + timedelta(days=UPCOMING_DAYS), "upcoming"


def _event_span(ev: dict) -> tuple[date, date]:
    start = date.fromisoformat(ev["start_date"])
    return start, date.fromisoformat(ev.get("end_date") or ev["start_date"])


def handle_calendar_question(state: AgentState) -> dict:
    user = state["user"]
    if not user:
        return _no_user()
    year = user.get("year") if user.get("role") == "student" else None
    from . import library  # library imports rag and backends; late keeps imports one-way

    res = library.events_in_force(year)
    if not res["success"]:
        return _out(
            "I couldn't reach the calendar just now. Please try again shortly.",
            error=res["error"], outcome="read_failed",
        )
    source, events = res["data"]["source"], res["data"]["events"]
    e = state["entities"]
    kind = e.get("calendar_kind") if e.get("calendar_kind") in CALENDAR_KINDS else None
    today = clock.today()
    lo, hi, mode = calendar_range(e, today)

    def overlaps(ev: dict) -> bool:
        if not ev.get("start_date"):
            return False
        s, t = _event_span(ev)
        return t >= lo and (hi is None or s <= hi)

    wanted = [ev for ev in events if overlaps(ev) and (kind is None or ev["kind"] in CALENDAR_KINDS[kind])]
    wanted.sort(key=lambda ev: (ev["start_date"], ev.get("title") or ""))

    titles = _document_titles({ev.get("document_id") for ev in wanted} - {None})
    trace = {
        "calendar": {
            "heard": {k: e[k] for k in ("calendar_kind", "month", "next_only", "week_offset",
                                        "start_date", "end_date") if e.get(k) is not None},
            "range": f"{lo.isoformat()} .. {hi.isoformat() if hi else 'open'}",
            "mode": mode,
            "source": source,
            "matched": len(wanted),
            **({"year": year} if year else {}),
        }
    }
    footer = _calendar_footer(source, titles)

    if mode == "day":
        holidays = dates.college_holidays(year)
        if lo.weekday() >= 5:
            verdict = f"**{_day(lo)}** is a {lo:%A} — no regular classes."
        elif not dates.is_working_day(lo, holidays):
            name = dates.holiday_name(lo, year) or "a holiday"
            verdict = f"**{_day(lo)}** is a holiday — {name}."
        else:
            verdict = f"**{_day(lo)}** is a working day."
        others = [ev for ev in wanted if ev["kind"] != "holiday"]
        if others:
            verdict += " Also on that day: " + "; ".join(ev["title"] for ev in others) + "."
        return _out(f"{verdict}{footer}", outcome="answered", **trace)

    noun = KIND_WORDS.get(kind, "events")
    if mode == "next":
        upcoming = [ev for ev in wanted if _event_span(ev)[1] >= today]
        if not upcoming:
            return _out(f"I don't see any more {noun} in the calendar after {_day(today)}.{footer}",
                        outcome="none_found", **trace)
        ev = upcoming[0]
        s, t = _event_span(ev)
        when = "Today" if s <= today <= t else _span(s, t)
        return _out(f"The next one: **{when}** — {ev['title']}.{footer}", outcome="answered", **trace)

    where = {
        "month": f"in {lo:%B %Y}",
        "week": f"in the week of {_day(lo)}",
        "range": f"between {_day(lo)} and {_day(hi)}" if hi else "",
        "upcoming": f"in the next {UPCOMING_DAYS} days",
    }[mode]
    if not wanted:
        return _out(f"No {noun} {where} in the calendar.{footer}", outcome="none_found", **trace)
    lines = [f"- **{_span(*_event_span(ev))}** — {ev['title']}" for ev in wanted[:MAX_LISTED]]
    more = f"\n\n…and {len(wanted) - MAX_LISTED} more." if len(wanted) > MAX_LISTED else ""
    head = f"{noun[:1].upper()}{noun[1:]} {where}:"
    return _out(f"{head}\n\n" + "\n".join(lines) + more + footer, outcome="answered", **trace)


def _document_titles(ids: set[str]) -> dict[str, str]:
    out = {}
    backend = backends.get_backend()
    for doc_id in sorted(ids):
        res = backend.get_document(doc_id)
        if res["success"] and res["data"]:
            out[doc_id] = res["data"]["title"]
    return out


def _calendar_footer(source: str, titles: dict[str, str]) -> str:
    if source == "seed":
        return ("\n\n*These come from the placeholder holiday list — no academic "
                "calendar has been confirmed yet, so check with your advisor.*")
    if titles:
        return "\n\n*From " + ", ".join(f"{t} ({i})" for i, t in titles.items()) + ".*"
    return "\n\n*From the College's academic calendar.*"


# ---------------------------------------------------------------------------
# file_complaint
# ---------------------------------------------------------------------------
def handle_file_complaint(state: AgentState) -> dict:
    user = state["user"]
    if not user:
        return _no_user()

    entities = state["entities"]
    category = entities.get("category")
    if not category:
        # Not guessed. Which category it is decides who sees it, and a
        # library complaint filed as hostel sits in the wrong inbox.
        return _out(
            "I can file that — is it about the hostel, a lab, the library, "
            "or transport?",
            outcome="needs_category",
        )

    # Default urgency is code's decision, not the model's guess.
    priority = entities.get("priority") or "medium"
    notes = (entities.get("description") or state["current_message"]).strip()

    backend = backends.get_backend()
    res = backend.create_complaint(
        user_id=user["id"], notes=notes, category=category, priority=priority
    )
    if not res["success"]:
        return _out(
            "I couldn't file that complaint — the records system didn't accept "
            f"it ({res['error']}). Nothing was created, so please try again "
            "shortly.",
            backend=backend.name, error=res["error"], outcome="write_failed",
        )

    r = res["data"]
    backend.append_event(r["id"], user["id"], "file", None, r["status"])
    return _out(
        f"Filed **{r['id']}** ({r['category']}, priority {r['priority']}) — "
        f'status {r["status"]}. I logged it as: "{r["notes"]}"',
        backend=res.get("backend", backend.name),
        record_id=r["id"],
        outcome="created",
    )


# ---------------------------------------------------------------------------
# Shared: fetch one record the caller named, and check it is theirs
# ---------------------------------------------------------------------------
def _own_record(user: dict, record_id: str, doing: str) -> tuple[dict | None, dict | None]:
    """(record, refusal). The owner-only gate for amend and cancel.

    Ownership is the whole rule here, not role. These are the two mutations
    a student may perform, and a staff member changing *someone else's*
    record goes through apply_staff_action — which checks the capability.
    (The predecessor let any staff account through this gate with
    `is_staff`, which meant a warden could withdraw a student's leave
    request by phrasing it as a cancellation. Two doors onto one record,
    only one of them locked.)

    The ownership check necessarily runs after the read: you cannot know
    whose record it is without fetching it.
    """
    backend = backends.get_backend()
    current = backend.get_record(record_id)
    if not current["success"]:
        return None, _out(
            f"I couldn't reach the records system, so nothing was {doing}.",
            backend=backend.name, error=current["error"], outcome="read_failed",
        )
    record = current["data"]
    if record is None:
        return None, _out(
            f"I don't see a record with the ID {record_id}.", outcome="not_found"
        )
    if record["user_id"] != user["id"]:
        who = (
            "Staff decisions on other people's records go through the staff "
            "actions instead."
            if auth.is_staff(user)
            else "Only the person who filed it can change it."
        )
        return None, _out(
            f"{record['id']} isn't yours, so I can't change it. {who}",
            authz="denied", reason="record owned by another user",
            outcome="not_authorized",
        )
    return record, None


# ---------------------------------------------------------------------------
# amend_record — "make it urgent", one turn after filing
# ---------------------------------------------------------------------------
def _latest_open(user_id: str, types: tuple[str, ...]) -> tuple[str | None, str | None]:
    """(record_id, problem_sentence). The record a bare follow-up means.

    Resolved here rather than by the classifier. The model is good at
    noticing that "make it urgent" is an amendment and bad at reliably
    carrying an id forward from four turns ago — and an id it invents is a
    write to someone else's record. Same split as dates: the model reports
    intent, code resolves the reference.
    """
    res = backends.get_backend().list_records_for_user(user_id, types)
    if not res["success"]:
        return None, (
            "I couldn't reach the records system, so I don't know which "
            "request you mean. Nothing was changed."
        )
    open_rows = [r for r in res["data"] or [] if _is_open(r)]
    if not open_rows:
        return None, (
            "You don't have anything open for me to change. If this is a new "
            "problem, describe it and I'll file a complaint."
        )
    return max(open_rows, key=_recency_key)["id"], None


def handle_amend_record(state: AgentState) -> dict:
    user = state["user"]
    if not user:
        return _no_user()

    entities = state["entities"]
    asked = {f: entities[f] for f in ("priority", "category", "reason") if entities.get(f)}
    if not asked:
        return _out(
            "What would you like me to change? On a complaint I can update the "
            "priority (low, medium or high) or the category; on a leave "
            "request, the reason.",
            outcome="needs_change",
        )

    # Which record. An explicit id wins; otherwise the most recent open
    # record of a type the requested change applies to.
    record_id = entities.get("record_id")
    resolved_via = "named in the message"
    if not record_id:
        types = tuple(t for t, fields in AMENDABLE.items() if set(asked) & set(fields))
        record_id, problem = _latest_open(user["id"], types)
        if problem:
            return _out(problem, outcome="needs_target", authz="allowed")
        resolved_via = "most recent open record"

    record, refusal = _own_record(user, record_id, "changed")
    if refusal:
        return refusal

    if not _is_open(record):
        return _out(
            f"{record['id']} is already **{record['status']}**, so it can't be "
            "changed any more.",
            outcome="terminal_state", authz="allowed",
        )

    allowed = AMENDABLE[record["type"]]
    changes = {k: v for k, v in asked.items() if k in allowed}
    if not changes:
        extra = (
            " New dates mean a new attendance projection, so withdraw this "
            "one and apply again."
            if record["type"] == OD_REQUEST
            else ""
        )
        return _out(
            f"{record['id']} is {TYPE_NAMES[record['type']]}; on those I can "
            f"only change the {' or '.join(allowed)}.{extra}",
            outcome="not_amendable", authz="allowed",
        )

    backend = backends.get_backend()
    res = backend.update_record(record["id"], **changes)
    if not res["success"]:
        return _out(
            f"I couldn't update {record['id']} ({res['error']}). It's unchanged.",
            backend=backend.name, error=res["error"], outcome="write_failed",
        )
    backend.append_event(
        record["id"], user["id"], "amend", record["status"], record["status"],
        note=", ".join(f"{k}={v}" for k, v in changes.items()),
    )

    r = res["data"]
    described = ", ".join(f"{k} is now **{v}**" for k, v in changes.items())
    return _out(
        f"Updated **{r['id']}** — {described}. It's still {r['status']}.",
        backend=res.get("backend", backend.name),
        record_id=r["id"], changed=changes, resolved_via=resolved_via,
        authz="allowed", reason="own record", outcome="updated",
    )


# ---------------------------------------------------------------------------
# cancel_record — withdrawing your own
# ---------------------------------------------------------------------------
def _cancel_own(user: dict, record_id: str) -> tuple[dict | None, dict | None]:
    """(updated_record, refusal) for one of the caller's own records."""
    record, refusal = _own_record(user, record_id, "cancelled")
    if refusal:
        return None, refusal
    if not _is_open(record):
        return None, _out(
            f"{record['id']} is already **{record['status']}**, so there's "
            "nothing to withdraw.",
            outcome="terminal_state", authz="allowed",
        )

    backend = backends.get_backend()
    new_status = OWNER_CANCEL_STATUS[record["type"]]
    res = backend.update_record(record["id"], status=new_status)
    if not res["success"]:
        return None, _out(
            f"I couldn't withdraw {record['id']} ({res['error']}). It's unchanged.",
            backend=backend.name, error=res["error"], outcome="write_failed",
        )
    backend.append_event(record["id"], user["id"], "withdraw", record["status"], new_status)
    return res["data"], None


def _describe_short(record: dict) -> str:
    if record["type"] == OD_REQUEST:
        return (
            f"**{record['id']}** — {LEAVE_NAMES.get(record['leave_kind'], 'leave')}, "
            f"{record['start_date']} to {record['end_date']}"
        )
    return f"**{record['id']}** — {record['category']}, {(record['notes'] or '')[:60]}"


def handle_cancel_record(state: AgentState) -> dict:
    user = state["user"]
    if not user:
        return _no_user()

    entities = state["entities"]
    record_id = entities.get("record_id")

    # A named record: withdraw exactly that one.
    if record_id:
        record, refusal = _cancel_own(user, record_id)
        if refusal:
            return refusal
        return _out(
            f"Done — **{record['id']}** is now **{record['status']}**. File "
            "again if you need it after all.",
            record_id=record["id"], authz="allowed", reason="own record",
            outcome="cancelled",
        )

    res = backends.get_backend().list_records_for_user(user["id"])
    if not res["success"]:
        return _out(
            "I couldn't reach the records system just now, so I haven't "
            "withdrawn anything.",
            outcome="read_failed",
        )
    rows = [r for r in res["data"] or [] if _is_open(r)]
    if not rows:
        return _out(
            "You don't have anything open to withdraw.",
            outcome="nothing_to_cancel", authz="allowed",
        )

    # "cancel all my complaints" — act on the lot, and name every one.
    if entities.get("applies_to_all") or len(rows) == 1:
        done, failed = [], []
        for row in sorted(rows, key=_recency_key):
            record, _ = _cancel_own(user, row["id"])
            (done if record else failed).append(row["id"])
        if not done:
            return _out(
                "I couldn't withdraw " + ", ".join(failed) + ". Nothing changed.",
                outcome="write_failed", authz="allowed",
            )
        listed = ", ".join(f"**{i}**" for i in done)
        tail = "" if not failed else " I couldn't withdraw " + ", ".join(failed) + ", though."
        return _out(
            f"Withdrawn: {listed}. File again if you need to." + tail,
            cancelled=done, outcome="cancelled", authz="allowed", reason="own records",
        )

    # Several, and they didn't say which. Ask rather than guess — this
    # writes, and picking the wrong one is not something a reply can undo.
    listing = "\n".join(f"- {_describe_short(r)}" for r in sorted(rows, key=_recency_key))
    return _out(
        f"You have {len(rows)} open — which should I withdraw?\n{listing}\n\n"
        'Give me the ID, or say "all of them".',
        outcome="needs_target", authz="allowed", candidates=[r["id"] for r in rows],
    )


# ---------------------------------------------------------------------------
# check_status — records and attendance
# ---------------------------------------------------------------------------
def _subject_of(
    user: dict, target_name: str | None, is_staff: bool
) -> tuple[dict | None, dict | None]:
    """Whose records this status check is about: (subject, refusal).

    Defaults to the caller. A name only *redirects* the lookup, and only
    for staff — for a student it is refused here, before any read.
    `target_person` is a lookup hint and never an authorization input:
    resolving "Ravi" to a person and then reading his records are two
    separate steps, and only the second is gated.
    """
    if not target_name:
        return user, None

    matches = users.find_by_name(target_name)
    if not matches or matches[0]["id"] == user["id"]:
        return user, None

    if not is_staff:
        return None, _out(
            f"I can't show you {matches[0]['name']}'s records. Students can "
            "only see their own; staff can look anyone up.",
            authz="denied", reason="cross-user lookup by a student",
            target=matches[0]["id"],
        )

    if len(matches) > 1:
        names = ", ".join(u["name"] for u in matches)
        return None, _out(
            f"I found more than one match for {target_name}: {names}. Whose "
            "records did you want?",
            authz="allowed", outcome="needs_target",
        )

    return matches[0], None


def _attendance_report(user: dict, subject: dict) -> dict:
    mine = subject["id"] == user["id"]
    res = backends.get_backend().get_attendance(subject["id"])
    if not res["success"]:
        return _out(
            "I couldn't reach the attendance records just now.",
            error=res["error"], outcome="read_failed",
        )
    row = res["data"]
    if row is None:
        return _out(
            f"There's no attendance on file for {'you' if mine else subject['name']}.",
            outcome="not_found",
        )

    proj = attendance.project(row["attended"], row["held"], 0, False)
    margin = attendance.days_of_margin(row["attended"], row["held"])
    who = "You are" if mine else f"{subject['name']} is"
    threshold = attendance.rule_pct(proj.threshold)
    if margin is None:
        tail = ""
    elif margin < 0:
        tail = f" That's below the {threshold} requirement."
    elif margin == 0:
        tail = (
            f" That's right at the {threshold} line — one more day of "
            "non-OD absence would put it below."
        )
    else:
        tail = (
            f" {_days(margin, capital=True)} of non-OD absence would still keep "
            f"{'you' if mine else 'them'} at or above {threshold}."
        )
    return _out(
        f"{who} at **{attendance.pct(proj.current_pct)}** — {row['attended']} of "
        f"{row['held']} periods attended"
        + (f", as of {row['as_of']}" if row.get("as_of") else "")
        + "."
        + tail,
        attendance={**row, "current_pct": proj.current_pct, "days_of_margin": margin},
        authz="allowed",
        **({} if mine else {"subject": subject["id"], "reason": f"role={user['role']}"}),
        outcome="found",
    )


def handle_check_status(state: AgentState) -> dict:
    user = state["user"]
    if not user:
        return _no_user()

    is_staff = auth.is_staff(user)
    entities = state["entities"]
    record_id = entities.get("record_id")

    subject, refusal = _subject_of(user, entities.get("target_person"), is_staff)
    if refusal:
        return refusal

    if entities.get("about_attendance") and not record_id:
        return _attendance_report(user, subject)

    backend = backends.get_backend()
    if record_id:
        res = backend.get_record(record_id)
        if not res["success"]:
            return _out(
                "I couldn't reach the records system, so I don't know its "
                "status right now.",
                backend=backend.name, error=res["error"], outcome="read_failed",
            )
        record = res["data"]
        if record is None:
            return _out(
                f"I don't see a record with the ID {record_id}. Double-check "
                "the number, or ask me to list your open requests.",
                outcome="not_found",
            )
        # The first authorization question: reading someone else's record.
        if record["user_id"] != user["id"] and not is_staff:
            return _out(
                f"{record['id']} belongs to another student, so I can't show it "
                "to you.",
                authz="denied", reason="record owned by another user",
            )
        return _out(
            _describe(record, users.get(record["user_id"])),
            backend=backend.name, record_id=record["id"], authz="allowed",
            outcome="found",
        )

    mine = subject["id"] == user["id"]
    res = backend.list_records_for_user(subject["id"])
    if not res["success"]:
        return _out(
            "I couldn't reach the records system just now. Please try again "
            "in a moment.",
            error=res["error"], outcome="read_failed",
        )

    rows = sorted(res["data"] or [], key=_recency_key, reverse=True)
    trace = {
        "backend": backend.name,
        "count": len(rows),
        "authz": "allowed",
        **({} if mine else {"subject": subject["id"], "reason": f"role={user['role']}"}),
    }
    if not rows:
        return _out(
            f"{'You don' if mine else subject['name'] + ' doesn'}'t have any "
            "complaints or leave requests on file.",
            outcome="found", **trace,
        )
    listing = "\n".join(f"- {_describe(r)}" for r in rows)
    return _out(
        f"Here's what I have for {'you' if mine else subject['name']}:\n{listing}",
        outcome="found", **trace,
    )


def _describe(record: dict, owner: dict | None = None) -> str:
    who = f" ({owner['name']})" if owner else ""
    if record["type"] == OD_REQUEST:
        span = f"{record['start_date']} to {record['end_date']}"
        line = (
            f"{record['id']}{who}: {LEAVE_NAMES.get(record['leave_kind'], 'leave')}, "
            f"{span} ({record.get('working_days')} working days) — "
            f"**{record['status']}**"
        )
        proj = record.get("projection") or {}
        if proj.get("projected_pct") is not None:
            line += f" · projected attendance {attendance.pct(proj['projected_pct'])}"
        if record.get("needs_exception") and _is_open(record):
            line += " · needs the advisor's exception"
        return line
    return (
        f"{record['id']}{who}: {record['category']}, priority "
        f"{record['priority']} — **{record['status']}** ({record['notes']})"
    )


# ---------------------------------------------------------------------------
# request_od_leave — the attendance projection, computed in Python
# ---------------------------------------------------------------------------
def _projection_sentence(proj: attendance.Projection, kind: str) -> str:
    """The sentence the whole domain change exists to produce.

    Every number in it falls out of attendance.project against the
    student's real figures. None of them is written here.
    """
    now = attendance.pct(proj.current_pct)
    then = attendance.pct(proj.projected_pct)
    need = attendance.rule_pct(proj.threshold)
    days = _days(proj.working_days, capital=True)
    what = LEAVE_NAMES[kind]

    if proj.condoned:
        lead = (
            f"You are at **{now}**. {what} counts as present, so {days.lower()} "
            f"of it takes you to **{then}**"
        )
    else:
        lead = f"You are at **{now}**. {days} of {what} takes you to **{then}**"

    if proj.below_threshold:
        return (
            f"{lead} — below the {need} requirement, so this request needs "
            "the advisor's exception."
        )
    return f"{lead}, which keeps you above the {need} requirement."


def handle_request_od_leave(state: AgentState) -> dict:
    user = state["user"]
    if not user:
        return _no_user()

    if user.get("role") != "student":
        return _out(
            "Leave requests here are tracked against a student's attendance, "
            "and staff accounts don't have any. Staff leave goes through the "
            "college office.",
            outcome="not_applicable", authz="allowed",
        )

    entities = state["entities"]
    kind = entities.get("leave_kind")
    raw_start, raw_end = entities.get("start_date"), entities.get("end_date")

    # Asked for, never guessed: whether the days are condoned depends on it.
    if not kind:
        return _out(
            "Is this OD (a college event, sports, a symposium), medical leave, "
            "or casual leave? It matters — only OD counts as present.",
            outcome="needs_leave_kind",
        )
    if not raw_start:
        return _out(
            f"Which dates do you need the {LEAVE_NAMES[kind]} for? For example, "
            '"next Monday to Wednesday" or "23 to 25 November".',
            outcome="needs_dates",
        )
    try:
        start = date.fromisoformat(raw_start)
        end = date.fromisoformat(raw_end) if raw_end else start
    except ValueError:
        return _out(
            "I couldn't read those dates. Could you give them as day and "
            "month — for example, 23 to 25 November?",
            outcome="bad_dates",
        )
    if end < start:
        return _out(
            f"That range ends before it starts ({_day(start)} to {_day(end)}). "
            "Which dates did you mean?",
            outcome="bad_dates",
        )
    if start < clock.today():
        return _out(
            f"{_day(start)} is in the past, so I can't apply for it. If you were "
            "absent unexpectedly, speak to your faculty advisor about it.",
            outcome="past_dates",
        )

    # The student's own year: a holiday some years get and others don't
    # (a first-year-only break) must count for exactly the right people.
    year = user.get("year")
    hols = dates.college_holidays(year)
    working = dates.working_days(start, end, hols)
    skipped = dates.holidays_between(start, end, hols)
    trace = {
        "span": f"{start.isoformat()} .. {end.isoformat()}",
        "working_days": working,
        "holiday_calendar": dates.holiday_source(),
        **(
            {"holidays_skipped": [f"{d.isoformat()} {dates.holiday_name(d, year) or ''}".strip()
                                  for d in skipped]}
            if skipped else {}
        ),
    }
    if working == 0:
        return _out(
            f"{_span(start, end)} has no working days in it — no classes to "
            "miss, so there's nothing to apply for.",
            outcome="no_working_days", **trace,
        )

    backend = backends.get_backend()
    att = backend.get_attendance(user["id"])
    if not att["success"]:
        return _out(
            "I couldn't reach the attendance records, so I can't work out what "
            "this does to your percentage. Nothing was submitted — please try "
            "again shortly.",
            error=att["error"], outcome="read_failed", **trace,
        )

    proj = None
    if att["data"] is not None:
        proj = attendance.project(
            att["data"]["attended"], att["data"]["held"], working,
            condoned=attendance.is_condoned(kind),
        )
        trace["attendance"] = proj.as_dict()

    # Always created. Falling short does not refuse the request; it flags it
    # for a human. The system informs, the advisor decides.
    needs_exception = bool(proj and proj.below_threshold)
    reason = entities.get("reason") or state["current_message"]
    res = backend.create_od_request(
        user_id=user["id"],
        leave_kind=kind,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        working_days=working,
        reason=reason,
        projection=proj.as_dict() if proj else None,
        needs_exception=needs_exception,
    )
    if not res["success"]:
        return _out(
            f"I couldn't save that request ({res['error']}). Nothing was "
            "submitted — please try again shortly.",
            outcome="write_failed", **trace,
        )

    r = res["data"]
    backend.append_event(
        r["id"], user["id"], "submit", None, r["status"],
        note="below attendance threshold" if needs_exception else None,
    )

    holiday_note = (
        " (" + ", ".join(
            f"{dates.holiday_name(d, year) or 'a holiday'} on {_day(d)}" for d in skipped
        ) + " isn't counted)"
        if skipped else ""
    )
    head = (
        f"Submitted **{r['id']}** — {LEAVE_NAMES[kind]}, {_span(start, end)}, "
        f"{_days(working)} of class{holiday_note}. Status **{r['status']}**; "
        "it isn't granted until your advisor approves it."
    )
    body = (
        _projection_sentence(proj, kind)
        if proj
        else "I couldn't find your attendance on file, so the advisor will "
        "need to check it by hand."
    )
    return _out(
        f"{head}\n\n{body}",
        backend=res.get("backend", backend.name),
        record_id=r["id"],
        needs_exception=needs_exception,
        authz="allowed",
        outcome="created",
        **trace,
    )


# ---------------------------------------------------------------------------
# staff_action — the RBAC gate
# ---------------------------------------------------------------------------
def apply_staff_action(
    user: dict | None, record_id: str, action: str, grant_exception: bool = False
) -> dict:
    """Perform one staff mutation — and the only implementation of that.

    Chat reaches here through handle_staff_action; the staff console reaches
    here through POST /api/records/{id}/action. Two doors, one lock. It
    re-checks the role itself rather than trusting whichever door called it,
    because a console that hides a button is a courtesy, not a mechanism.

    The order of the checks is deliberate (README, invariant 3):

      1. Students are refused first — they hold no capability, so nothing
         about the record could change the answer. It is also the cheapest
         check, and it short-circuits before any read.
      2. The verb against the record type. "A complaint cannot be approved"
         is a fact about the record, true whoever asks.
      3. Then the caller's capability against the record type.

    Putting the capability check before the type check makes the type check
    unreachable for every real role — nobody holds both capabilities — and
    therefore untestable. There is a test that depends on this order.

    After those come two facts about the record's *state*: a terminal record
    cannot be actioned, and a request flagged for an attendance shortage
    cannot be approved without an explicit exception.

    Returns a backend-style envelope so both callers can read one result:
    chat turns it into a sentence, the console turns it into JSON.
    """
    # 1. Students: refused before anything is read.
    if not auth.is_staff(user):
        return backends.fail(
            "approving, rejecting, resolving or cancelling other people's "
            "requests is limited to staff accounts",
            backends.INVALID,
            outcome="not_authorized",
            authz="denied",
            reason=f"role={(user or {}).get('role', 'anonymous')} holds no capability",
            checked_against="signed session",
        )

    if action not in ACTION_STATUS:
        return backends.fail(
            f"{action!r} is not an action I can take.",
            backends.INVALID,
            outcome="unknown_action",
            authz="allowed",
        )

    backend = backends.get_backend()
    current = backend.get_record(record_id)
    if not current["success"]:
        return backends.fail(
            current["error"], current["kind"],
            outcome="read_failed", backend=backend.name, authz="allowed",
        )
    record = current["data"]
    if record is None:
        return backends.fail(
            f"No record {record_id} exists.", backends.NOT_FOUND,
            outcome="not_found", authz="allowed",
        )
    record_type = record.get("type")

    # 2. Verb against type — a fact about the record.
    if record_type not in ACTION_APPLIES_TO[action]:
        fits = sorted(a for a, types in ACTION_APPLIES_TO.items() if record_type in types)
        return backends.fail(
            f"{record['id']} is {TYPE_NAMES.get(record_type, 'a record')}, so it "
            f"can't be {ACTION_STATUS[action]} — try {' or '.join(fits)} instead.",
            backends.INVALID,
            outcome="wrong_action_for_type",
            authz="allowed",
        )

    # 3. Capability against type — a fact about the person, and the only
    # check that can refuse a *staff* user. It needs the record in hand.
    if not auth.may_action(user, record_type):
        return backends.fail(
            f"{record['id']} is {TYPE_NAMES.get(record_type, 'a record')}, and "
            f"{TYPE_PLURAL.get(record_type, 'those')} are decided by "
            f"{_who_can(record_type)} — not "
            f"{ROLE_LABEL.get(user.get('role'), 'this account')}.",
            backends.INVALID,
            outcome="not_authorized",
            authz="denied",
            reason=f"role={user.get('role')} may not action {record_type}",
            checked_against="signed session",
        )

    # State: "already resolved" is not "you may not".
    if not _is_open(record):
        return backends.fail(
            f"{record['id']} is already **{record['status']}**, so it can't be "
            f"{ACTION_STATUS[action]} now.",
            backends.INVALID,
            outcome="terminal_state",
            authz="allowed",
            status=record["status"],
        )

    # The exception gate. The system flagged the shortfall; only a person
    # can decide to approve anyway, and they have to say so.
    proj = record.get("projection") or {}
    if action == "approve" and record.get("needs_exception") and not grant_exception:
        return backends.fail(
            f"{record['id']} would take the student from "
            f"{attendance.pct(proj.get('current_pct'))} to "
            f"{attendance.pct(proj.get('projected_pct'))}, below the "
            f"{attendance.rule_pct(proj.get('threshold'))} requirement. Approving it "
            "means granting an attendance exception — confirm that's intended.",
            backends.INVALID,
            outcome="needs_exception_ack",
            authz="allowed",
            projection=proj,
        )

    new_status = ACTION_STATUS[action]
    res = backend.update_record(
        record["id"],
        status=new_status,
        decided_by=user["id"],
        decided_at=clock.today().isoformat(),
    )
    if not res["success"]:
        return backends.fail(
            res["error"], res["kind"],
            outcome="write_failed", backend=backend.name, authz="allowed",
        )

    granted = action == "approve" and bool(record.get("needs_exception"))
    event = backend.append_event(
        record["id"], user["id"], action, record["status"], new_status,
        note="attendance exception granted" if granted else None,
    )
    return backends.ok(
        res["data"],
        outcome="updated",
        action=action,
        exception_granted=granted or None,
        event_logged=event["success"],
        authz="allowed",
        reason=f"role={user.get('role')} may action {record_type}",
        checked_against="signed session",
        backend=res.get("backend", backend.name),
    )


# Which record type a staff verb means when only a name was given.
ACTION_LOOKS_AT = {
    "approve": (OD_REQUEST,),
    "reject": (OD_REQUEST,),
    "resolve": (COMPLAINT,),
    "cancel": (OD_REQUEST, COMPLAINT),
}


def _find_target_record(name: str | None, action: str) -> tuple[str | None, str | None]:
    """Resolve "Ravi" to the record this action means: (record_id, problem).

    Which record depends on the verb — "resolve Ravi's" is about a
    complaint, "approve Ravi's" about leave. Resolving the name is only a
    lookup; whether the caller may act on what it finds is apply_staff_action's
    question, asked separately.
    """
    if not name:
        return None, None

    matches = users.find_by_name(name)
    if not matches:
        return None, f"I couldn't find anyone called {name}."
    if len(matches) > 1:
        names = ", ".join(u["name"] for u in matches)
        return None, f"I found more than one match for {name}: {names}. Which one?"

    target = matches[0]
    types = ACTION_LOOKS_AT.get(action, (OD_REQUEST, COMPLAINT))
    res = backends.get_backend().list_records_for_user(target["id"], types)
    if not res["success"]:
        return None, f"I couldn't look up {target['name']}'s records just now."
    candidates = [r for r in res["data"] or [] if _is_open(r)]
    if not candidates:
        wanted = " or ".join(
            "pending leave requests" if t == OD_REQUEST else "open complaints" for t in types
        )
        return None, f"{target['name']} has no {wanted} to {action}."

    # Most recent wins, same rule as a bare amendment.
    return max(candidates, key=_recency_key)["id"], None


def handle_staff_action(state: AgentState) -> dict:
    user = state["user"]
    if not user:
        return _no_user()

    # The sentence-producing gate. apply_staff_action repeats this check,
    # which is intentional: this one exists to produce a helpful reply, that
    # one to make the rule unskippable.
    if not auth.is_staff(user):
        return _out(
            "I can't do that — approving, rejecting and resolving requests is "
            "for staff, and yours is a student account. "
            f"{_who_can(OD_REQUEST)[:1].upper() + _who_can(OD_REQUEST)[1:]} "
            f"decides leave; {_who_can(COMPLAINT)} handles complaints.",
            authz="denied",
            reason=f"role={user['role']} holds no capability",
            checked_against="signed session",
            outcome="not_authorized",
        )

    entities = state["entities"]
    action = entities.get("action")
    if not action:
        # Not defaulted. The predecessor assumed "approve" when no verb was
        # heard, which is a guess that writes.
        return _out(
            "What should I do with it — approve, reject, resolve, or cancel?",
            outcome="needs_action", authz="allowed",
        )

    # A staff account acting outside its remit is told so before we go
    # looking, rather than "Ravi has no open complaints to resolve" — true,
    # and an answer about the wrong thing. `cancel` spans both types, so it
    # falls through to the post-read check, which knows what it got.
    needed = ACTION_APPLIES_TO.get(action, set())
    if needed and not any(auth.may_action(user, t) for t in needed):
        kind = next(iter(needed))
        return _out(
            f"I can't {action} that — {TYPE_PLURAL.get(kind, 'those records')} "
            f"are decided by {_who_can(kind)}, and your account is "
            f"{ROLE_LABEL.get(user['role'], user['role'])}.",
            authz="denied",
            reason=f"role={user['role']} may not action {kind}",
            checked_against="signed session",
            outcome="not_authorized",
        )

    record_id = entities.get("record_id")
    if not record_id:
        record_id, problem = _find_target_record(entities.get("target_person"), action)
        if problem:
            return _out(problem, authz="allowed", outcome="needs_target")
        if record_id is None:
            return _out(
                "Which request should I action? Give me the ID (like OD-2001) "
                "or the student's name.",
                authz="allowed", outcome="needs_target",
            )

    res = apply_staff_action(
        user, record_id, action, grant_exception=bool(entities.get("grant_exception"))
    )
    trace = _trace(res)

    if not res["success"]:
        outcome = res.get("outcome")
        if outcome == "not_found":
            return _out(
                f"I don't see a record with the ID {record_id}, so there was "
                f"nothing to {action}.",
                **trace,
            )
        if outcome == "read_failed":
            return _out(
                "I couldn't reach the records system, so I haven't changed "
                "anything.",
                **trace,
            )
        if outcome == "needs_exception_ack":
            return _out(
                res["error"] + ' Say "approve it as an exception" to go ahead, '
                "or reject it.",
                **trace,
            )
        if outcome in {"wrong_action_for_type", "not_authorized", "terminal_state"}:
            return _out(res["error"], **trace)  # already a complete sentence
        return _out(
            f"I couldn't {action} {record_id} ({res['error']}). Its status is "
            "unchanged.",
            **trace,
        )

    r = res["data"]
    owner = users.get(r["user_id"])
    exception = ", as an attendance exception" if res.get("exception_granted") else ""
    return _out(
        f"Done — **{r['id']}** ({owner['name'] if owner else r['user_id']}) is "
        f"now **{r['status']}**{exception}. Logged against your account, "
        f"{user['name']}.",
        record_id=r["id"],
        **trace,
    )


# ---------------------------------------------------------------------------
# clarify / out_of_scope — the messy cases
# ---------------------------------------------------------------------------
def _history_messages(state: AgentState, turns: int) -> list[dict]:
    return [
        {
            "role": "assistant" if t.get("role") in ("bot", "assistant") else "user",
            "content": t.get("content", ""),
        }
        for t in state["history"][-turns:]
    ]


def handle_clarify(state: AgentState) -> dict:
    """Ask ONE specific question. Vague help ("what do you need?") just
    bounces the ambiguity back at the user. A designed outcome, not an error."""
    try:
        text, provider = llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are Compass, the College's campus service desk. "
                        "The user's message was too vague to act on. Ask "
                        "exactly one specific clarifying question that would "
                        "let you route it. Offer two or three concrete options "
                        f"drawn from what you can do. {CAPABILITIES} Two "
                        "sentences maximum. Never invent records, statuses or "
                        "numbers."
                    ),
                },
                *_history_messages(state, 4),
                {"role": "user", "content": state["current_message"]},
            ],
            temperature=0.3,
            max_tokens=90,
        )
        return _out(text, provider=provider, outcome="clarified")
    except Exception as e:  # noqa: BLE001 — a fallback sentence beats a 500
        return _out(
            "I want to make sure I get this right — is this about a facility "
            "complaint, an OD or leave request, or a question about the "
            "regulations?",
            error=f"{type(e).__name__}: {e}",
            outcome="clarified_fallback",
        )


def handle_out_of_scope(state: AgentState) -> dict:
    try:
        text, provider = llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are Compass, the College's campus service desk. "
                        "This message is outside what you do. If it's a "
                        "greeting or small talk, answer warmly and briefly. "
                        "Otherwise decline plainly — no apology spiral. Either "
                        f"way, say what you can help with. {CAPABILITIES} Two "
                        "sentences maximum. Never claim you did something."
                    ),
                },
                {"role": "user", "content": state["current_message"]},
            ],
            temperature=0.4,
            max_tokens=90,
        )
        return _out(text, provider=provider, outcome="declined")
    except Exception as e:  # noqa: BLE001
        return _out(
            f"That's outside what I can do, I'm afraid. {CAPABILITIES}",
            error=f"{type(e).__name__}: {e}",
            outcome="declined_fallback",
        )


# ---------------------------------------------------------------------------
# respond — the single exit
# ---------------------------------------------------------------------------
def respond(state: AgentState) -> dict:
    """Last node before END. Guarantees a reply exists and stamps the trace."""
    reply = (state.get("reply") or "").strip()
    if not reply:
        reply = (
            "Something went wrong on my side and I don't have an answer for "
            "you. Nothing was changed."
        )
    return {"reply": reply, "debug": {"handled_by": state.get("intent")}}
