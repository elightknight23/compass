"""The LangGraph orchestration: classify -> route -> act -> respond.

The classifier is the agent's nervous system, so it does not get to
improvise. Every routing decision comes back through a Pydantic schema
via structured outputs, which means the API — not a prompt suggestion —
guarantees `intent` is one of the nine the graph has an edge for. A
hallucinated intent is structurally impossible rather than merely
unlikely, and the conditional edge can never KeyError.

The same reasoning applies one level down: `category`, `leave_kind` and
`action` are Literal unions, not free strings, so the model cannot invent
"canteen" when the complaint taxonomy says hostel, lab, library or transport.

**This is the only creative step in a turn.** The classifier reports what
it *heard* — an intent, a record id, a name, the parts of a date expression
— and never what follows from it. It does not compute dates, percentages or
eligibility, and it does not decide permission. Everything after this node
is ordinary Python.

LLMs are frozen in time, so the system prompt is rebuilt on every
request with today's date. The model still does not compute dates — but
it needs to know what "today" is to tell a literal date ("23 November")
from a relative one, and to read "this Friday" against the right week.
"""

from __future__ import annotations

import time
from typing import Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, model_validator

from . import clock, dates, handlers, llm
from .dates import DateSpec
from .state import AgentState, Intent

WeekdayName = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]

HISTORY_TURNS = 6  # sliding window fed to the classifier
CONFIDENCE_FLOOR = 0.45  # below this, ask instead of guessing


# ---------------------------------------------------------------------------
# The schema the model must answer in
# ---------------------------------------------------------------------------
_NULLISH = {"", "null", "none", "n/a"}

MonthName = Literal[
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
]


class Classification(BaseModel):
    """Structured output contract for one user message."""

    @model_validator(mode="before")
    @classmethod
    def _blank_means_absent(cls, data):
        """Models often say "" or "null" for a field they mean to leave out.
        For an optional field that is absence, and must be None: "" in a
        Literal field would otherwise fail validation and turn the whole
        turn into a clarify, and "" in a date field would reach the date
        parser. Required fields are left alone so a genuinely broken reply
        still fails loudly."""
        if not isinstance(data, dict):
            return data
        optional = {n for n, f in cls.model_fields.items() if f.default is None}
        return {
            k: (None if k in optional and isinstance(v, str) and v.strip().lower() in _NULLISH else v)
            for k, v in data.items()
        }

    intent: Intent
    confidence: float = Field(description="0.0-1.0 certainty in the intent")
    reasoning: str = Field(description="One short sentence — shown in the trace")

    record_id: str | None = Field(
        default=None,
        description="A record id the user actually said, e.g. CMP-1001 or OD-2001",
    )
    target_person: str | None = Field(
        default=None,
        description=(
            "The person whose records the user is asking about, if they name "
            "someone other than themselves ('Ravi's OD request' -> 'Ravi'). "
            "A lookup hint only — never the actor."
        ),
    )
    # Dates are reported in parts, not computed. See app/dates.py for why:
    # the model reliably mis-resolves "Monday through Friday, four weeks
    # from now", so it now says what it heard and Python does the calendar.
    explicit_start_date: str | None = Field(
        default=None,
        description=(
            "ISO YYYY-MM-DD, ONLY when the user named a literal calendar date "
            "such as 'August 14'. Leave null for relative expressions."
        ),
    )
    explicit_end_date: str | None = Field(
        default=None, description="ISO YYYY-MM-DD, same rule as explicit_start_date"
    )
    day_offset: int | None = Field(
        default=None,
        description=(
            "Days from today when the leave starts, for offsets counted in "
            "days: today=0, tomorrow=1, 'in three days'=3. Null otherwise."
        ),
    )
    anchor_weekday: WeekdayName | None = Field(
        default=None, description="The weekday the leave STARTS on, if one is named"
    )
    week_offset: int | None = Field(
        default=None,
        description=(
            "Which week that weekday falls in, counting from the current "
            "week: 0=this week, 1='next Monday', 4='four weeks from now'."
        ),
    )
    end_weekday: WeekdayName | None = Field(
        default=None, description="The weekday the leave ENDS on, if one is named"
    )
    duration_weekdays: int | None = Field(
        default=None,
        description=(
            "How many working days off, when stated as a count rather than "
            "an end day: 'three days off'=3. Null if an end weekday is given."
        ),
    )
    in_past: bool = Field(
        default=False,
        description=(
            "True when the user describes days already gone: 'I was sick on "
            "Monday', 'I missed yesterday', 'I've been ill since Tuesday'."
        ),
    )
    category: Literal["hostel", "lab", "library", "transport"] | None = Field(
        default=None,
        description=(
            "Facility complaint category: hostel (rooms, mess, water, power "
            "in the hostel), lab, library, transport (college buses)."
        ),
    )
    leave_kind: Literal["od", "medical", "family_emergency", "personal"] | None = Field(
        default=None,
        description=(
            "od = on-duty (college-sanctioned: symposiums, sports, NSS, "
            "placement drives); medical = illness or injury; family_emergency "
            "= a serious emergency in the family (bereavement, a relative in "
            "hospital); personal = anything else (a family function, a "
            "wedding, a trip home, 'casual leave'). Null if the user did not say."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="The reason for leave in the user's own words, if given",
    )
    description: str | None = Field(
        default=None,
        description=(
            "For file_complaint: the problem being reported, in the user's "
            "own words. If this message only completes an earlier one (e.g. "
            "it just answers 'hostel'), restate the problem from the earlier "
            "turn. Do not embellish."
        ),
    )
    about_attendance: bool = Field(
        default=False,
        description=(
            "True when a check_status message asks for an attendance figure "
            "('what's my attendance?', 'how short is Ravi?')."
        ),
    )
    priority: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description=(
            "Urgency the user actually stated — 'this is urgent' or 'make it "
            "high' is high, 'no rush' is low. Null if they did not say, so "
            "that the default is applied by code rather than guessed here."
        ),
    )
    action: Literal["approve", "reject", "resolve", "cancel"] | None = Field(
        default=None,
        description=(
            "The staff operation requested. approve/reject decide an OD or "
            "leave request; resolve closes a facility complaint whose work "
            "is done ('the fan's been fixed, close it'); cancel withdraws "
            "either. Null if the user named no operation."
        ),
    )
    applies_to_all: bool = Field(
        default=False,
        description=(
            "True only when the user asked to act on ALL of their matching "
            "records at once — 'cancel all my complaints', 'withdraw them "
            "all'. False for a single record."
        ),
    )
    grant_exception: bool = Field(
        default=False,
        description=(
            "True only when a staff member explicitly says to approve AS AN "
            "EXCEPTION despite an attendance shortage ('approve it as an "
            "exception', 'grant the exception'). False otherwise."
        ),
    )

    # -- document_question: what to search. Heard, never decided: the code
    # turns these and the asker's profile into the actual filter.
    doc_scope: Literal["regulations", "syllabus", "calendar"] | None = Field(
        default=None,
        description=(
            "For document_question: syllabus = subjects, units, text books, "
            "reference books, credits, course codes; regulations = rules and "
            "policies; calendar = what the academic calendar document says. "
            "Null if unclear."
        ),
    )
    branch: str | None = Field(
        default=None,
        description=(
            "A branch/department the user NAMED, e.g. 'ECE's syllabus' -> 'ECE', "
            "'for computer science' -> 'computer science'. Null if they named none "
            "('my syllabus' is null — the code knows their branch)."
        ),
    )
    semester: int | None = Field(
        default=None,
        description="A semester number the user NAMED: '5th sem' -> 5. Null otherwise.",
    )
    my_courses: bool = Field(
        default=False,
        description=(
            "True when the user means their OWN current courses: 'my subjects', "
            "'this semester', 'my textbooks'."
        ),
    )

    # -- calendar_question: which events, and when.
    calendar_kind: Literal["holiday", "exam", "semester", "any"] | None = Field(
        default=None,
        description=(
            "For calendar_question: holiday (holidays, vacations, days off), exam "
            "(CAT, model, end-semester exams), semester (reopening, last working "
            "day), any (everything, or a single-day 'is there college on X?')."
        ),
    )
    month: MonthName | None = Field(
        default=None,
        description="A month the user named without a day: 'holidays in November' -> november.",
    )
    next_only: bool = Field(
        default=False,
        description="True for 'the NEXT holiday' / 'when is the next exam' — one event, the soonest.",
    )


CLASSIFIER_RULES = """You are the intent classifier for Compass, the College's \
campus service desk. Students, hostel wardens and faculty advisors use it. \
Classify the user's latest message into exactly one intent.

document_question   - asks what the College's documents SAY: a RULE (attendance,
                      OD and leave policy, exams, hostel, library, transport), the
                      SYLLABUS (subjects, units, text books, reference books,
                      credits), or what the calendar document says in general.
                      "What's the minimum attendance?", "Is OD counted as present?",
                      "What's the textbook for Computer Networks?", "Units in DBMS?"
calendar_question   - asks WHEN something dated happens, or whether a day is a
                      holiday: holidays, exam dates, semester start/end.
                      "When is the next holiday?", "Any holidays in November?",
                      "Is there college on Friday?", "When do CAT exams start?"
file_complaint      - reports a NEW facility problem: hostel, lab, library, transport.
                      "The fan in room B-214 isn't working", "Bus 7 didn't come"
amend_record        - CHANGES something the user already filed. No new problem is
                      being reported; they are adjusting one already on file.
                      "make it urgent", "that's actually a lab issue"
cancel_record       - WITHDRAWS the user's OWN complaint or leave request.
                      "cancel my complaint, it's fixed now", "I don't need that OD any more"
                      If they name SOMEONE ELSE's record, that is staff_action.
check_status        - asks about an EXISTING record's state, theirs or someone's.
                      "Status of OD-2001?", "Any update on my complaints?"
                      Also: "what's my attendance?"
request_od_leave    - applies for OD, medical, family-emergency or any other leave
                      for themselves, including leave already taken ("I was sick
                      yesterday").
                      "I need OD next Monday to Wednesday for a symposium",
                      "Apply for medical leave tomorrow"
staff_action        - asks to approve/reject/resolve a record, usually another
                      person's, or to close one because the work is DONE.
                      Classify on what is ASKED, never on whether the user is
                      allowed — permission is checked later, in code.
                      "Approve Ravi's OD request", "Reject OD-2003",
                      "Mark CMP-1002 resolved, the plumber came"
clarify             - genuinely ambiguous; you cannot tell which intent applies.
                      "I need help", "it's broken again", "what about mine?"
out_of_scope        - greetings, small talk, questions about Compass itself, and
                      anything outside campus services. "Hi", "who are you?",
                      "write my assignment"

Rules:
- Asking what a rule is = document_question. Asking to APPLY it = the action intent.
  "How many days of OD can I take?" -> document_question.
  "Put in OD for Friday" -> request_od_leave.
- WHEN a dated event is = calendar_question; what a document SAYS = document_question.
  "When is the next holiday?" -> calendar_question, calendar_kind=holiday, next_only.
  "Holidays in November?" -> calendar_question, calendar_kind=holiday, month=november.
  "Is Friday a holiday?" / "Is there college on Friday?" -> calendar_question,
    calendar_kind=any, anchor_weekday=friday, week_offset=0.
  "Any exams next week?" -> calendar_question, calendar_kind=exam, week_offset=1
    (no weekday: leave anchor_weekday null).
  "What is the attendance requirement?" -> document_question, doc_scope=regulations.
- Syllabus questions are document_question with doc_scope=syllabus. Fill branch or
  semester ONLY if the user named one; "my"/"this semester" is my_courses=true.
    "Textbook for Computer Networks?"     -> doc_scope=syllabus
    "What are my subjects this semester?" -> doc_scope=syllabus, my_courses=true
    "ECE 3rd sem syllabus for signals"    -> doc_scope=syllabus, branch=ECE, semester=3
- Asking for one's OWN attendance figure is check_status, not document_question.
- A new problem is file_complaint; an existing one is check_status.
- cancel_record vs staff_action turns on WHOSE record and WHY, not the verb.
  Withdrawing something because it is no longer wanted is cancel_record.
  Closing something because the WORK IS DONE is staff_action — that is the
  warden's judgement, not the requester's. Asking to CHANGE someone else's
  record is staff_action; merely asking to LOOK AT one is still check_status.
    "cancel my complaint" / "I don't need that leave"   -> cancel_record
    "resolve CMP-1002, the electrician fixed it"         -> staff_action
    "never mind, close CMP-1003"                         -> cancel_record
  A withdrawal cue ("never mind", "forget it", "no longer needed") outranks
  the verb.
- READ THE CONVERSATION ABOVE. A short follow-up almost always refers to the
  record just discussed, not to a new one.
    "The water heater on my floor is broken"  -> file_complaint
    then "make it high priority"              -> amend_record, priority=high
  If the user names no id, leave record_id null — the code resolves it to
  their most recent open record. Do not invent an id, and do not copy one out
  of the conversation unless the user actually said it.
- priority: fill it in whenever the user states urgency. "urgent"/"high" =
  high, "no rush" = low. Leave it null when they say nothing; the default is
  code's to choose.
- leave_kind: "OD", "on duty", a college event, sports, a symposium, a
  placement drive = od. The student's own illness or injury = medical. A
  bereavement or a family member in hospital = family_emergency. A family
  function, a wedding, travel, "casual leave", "personal reasons" = personal.
  Null if unclear — do not guess.
- If this message only COMPLETES an earlier request — answering a question
  Compass just asked, like "OD" or "hostel" — classify it as that earlier
  request and carry forward the parts the user said earlier (dates, reason,
  the problem). Carrying forward what was said is fine; computing is not.
- Prefer clarify over a confident guess. Asking is the safe failure; acting on
  the wrong intent is not.
- NEVER compute anything. No dates, no attendance percentages, no eligibility.
  Report the parts you heard and the code does the arithmetic.
- Dates: DO NOT do calendar arithmetic. Report the parts you heard and let
  the code compute the dates. You are reliably wrong at this; the code is not.
    "I need tomorrow off"          -> day_offset=1
    "I was sick yesterday"         -> day_offset=-1, in_past=true
    "I was ill on Monday and Tuesday"
                                   -> anchor_weekday=monday, week_offset=0,
                                      end_weekday=tuesday, in_past=true
    "I missed last Thursday"       -> anchor_weekday=thursday, week_offset=-1,
                                      in_past=true
    "next Monday through Wednesday" -> anchor_weekday=monday, week_offset=1,
                                       end_weekday=wednesday
    "Monday through Friday, four weeks from now"
                                   -> anchor_weekday=monday, week_offset=4,
                                      end_weekday=friday
    "three days starting Tuesday"  -> anchor_weekday=tuesday, week_offset=0,
                                      duration_weekdays=3
    "November 23 to 25"            -> explicit_start_date/explicit_end_date
  Use explicit_*_date ONLY for literal calendar dates the user actually said.
  week_offset counts from the current week: 0 = this week, 1 = next week.
  Leave every date field null when the message mentions no dates at all.
  Fill in exactly ONE way of saying when leave STARTS — an explicit date, OR
  a day_offset, OR an anchor_weekday. Never more than one. In particular, if
  a weekday is named, leave day_offset null: working out how many days away
  that weekday is IS the arithmetic you are being asked not to do.
- target_person: fill in only when the user names someone else. "my", "I", "me"
  always mean the current user, so leave it null."""


def _system_prompt(user: dict | None) -> str:
    """Rebuilt per request — the date is the whole point."""
    today = clock.today()
    who = (
        f"{user['name']}, role={user['role']}, department={user.get('department')}"
        if user
        else "an unidentified user"
    )
    return (
        f"{CLASSIFIER_RULES}\n\n"
        f"Today is {today.isoformat()}, a {today.strftime('%A')}.\n"
        f"You are classifying a message from {who}."
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def classify(state: AgentState) -> dict:
    """Turn one message into an intent plus entities."""
    started = time.perf_counter()
    messages: list[dict] = [{"role": "system", "content": _system_prompt(state["user"])}]
    for turn in state["history"][-HISTORY_TURNS:]:
        role = "assistant" if turn.get("role") in ("bot", "assistant") else "user"
        messages.append({"role": role, "content": turn.get("content", "")})
    messages.append({"role": "user", "content": state["current_message"]})

    try:
        result, provider = llm.parse(messages, Classification, temperature=0)
    except Exception as e:  # every provider down, or out of quota — never crash the turn
        return {
            "intent": Intent.CLARIFY.value,
            "confidence": 0.0,
            "entities": {},
            "debug": {
                "classifier_error": f"{type(e).__name__}: {e}",
                "intent": Intent.CLARIFY.value,
                "classify_ms": round((time.perf_counter() - started) * 1000),
            },
        }

    # The model reported date *parts*; Python turns them into real dates.
    spec = DateSpec(
        explicit_start=result.explicit_start_date,
        explicit_end=result.explicit_end_date,
        day_offset=result.day_offset,
        anchor_weekday=result.anchor_weekday,
        week_offset=result.week_offset,
        end_weekday=result.end_weekday,
        duration_weekdays=result.duration_weekdays,
        past=result.in_past,
    )
    # "three days starting Tuesday" counts working days, so it needs the
    # asker's holidays: a student's year decides which ones apply.
    year = (state["user"] or {}).get("year")
    resolved = None if spec.is_empty() else dates.resolve(spec, holidays=dates.college_holidays(year))

    entities = {
        k: v
        for k, v in {
            "record_id": result.record_id,
            "target_person": result.target_person,
            "start_date": resolved[0].isoformat() if resolved else None,
            "end_date": resolved[1].isoformat() if resolved else None,
            "category": result.category,
            "priority": result.priority,
            "action": result.action,
            "leave_kind": result.leave_kind,
            "reason": result.reason,
            "grant_exception": result.grant_exception or None,
            "description": result.description,
            "about_attendance": result.about_attendance or None,
            "doc_scope": result.doc_scope,
            "branch": result.branch,
            "semester": result.semester,
            "my_courses": result.my_courses or None,
            "calendar_kind": result.calendar_kind,
            "month": result.month,
            "next_only": result.next_only or None,
            # "next week" with no weekday: resolve() needs an anchor, so the
            # bare week is carried for the calendar handler to turn into a
            # Monday-to-Sunday span.
            "week_offset": (
                result.week_offset
                if result.week_offset is not None and not result.anchor_weekday
                and not result.explicit_start_date and result.day_offset is None
                else None
            ),
            # Only carried when true — a False here would look like a
            # deliberate "just one" in the trace, which it isn't.
            "applies_to_all": result.applies_to_all or None,
        }.items()
        if v is not None
    }

    # Both halves go in the trace: what the model heard, and what the
    # code made of it. The demo shows the seam.
    date_trace = (
        {
            "heard": {
                k: v
                for k, v in vars(spec).items()
                if v is not None
            },
            "resolved": (
                f"{resolved[0].isoformat()} ({resolved[0]:%a}) .. "
                f"{resolved[1].isoformat()} ({resolved[1]:%a})"
                if resolved
                else "no dates in message"
            ),
        }
        if not spec.is_empty()
        else None
    )
    confidence = max(0.0, min(1.0, result.confidence))
    intent = result.intent.value

    # A low-confidence guess is still a guess. Fall back to asking.
    downgraded = False
    if confidence < CONFIDENCE_FLOOR and intent != Intent.OUT_OF_SCOPE.value:
        intent, downgraded = Intent.CLARIFY.value, True

    return {
        "intent": intent,
        "confidence": confidence,
        "entities": entities,
        "debug": {
            "intent": intent,
            "confidence": round(confidence, 2),
            "reasoning": result.reasoning,
            "entities": entities,
            "classify_ms": round((time.perf_counter() - started) * 1000),
            "provider": provider,
            **({"dates": date_trace} if date_trace else {}),
            **(
                {"downgraded_from": result.intent.value, "reason": "low confidence"}
                if downgraded
                else {}
            ),
        },
    }


def route(state: AgentState) -> str:
    """Conditional edge: the intent IS the node name."""
    intent = state.get("intent") or Intent.CLARIFY.value
    return intent if intent in HANDLERS else Intent.CLARIFY.value


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
HANDLERS = {
    Intent.DOCUMENT_QUESTION.value: handlers.handle_document_question,
    Intent.CALENDAR_QUESTION.value: handlers.handle_calendar_question,
    Intent.FILE_COMPLAINT.value: handlers.handle_file_complaint,
    Intent.AMEND_RECORD.value: handlers.handle_amend_record,
    Intent.CANCEL_RECORD.value: handlers.handle_cancel_record,
    Intent.CHECK_STATUS.value: handlers.handle_check_status,
    Intent.REQUEST_OD_LEAVE.value: handlers.handle_request_od_leave,
    Intent.STAFF_ACTION.value: handlers.handle_staff_action,
    Intent.CLARIFY.value: handlers.handle_clarify,
    Intent.OUT_OF_SCOPE.value: handlers.handle_out_of_scope,
}

# Every Intent must have a node, or route() could dead-end at runtime. This
# is what makes renaming an intent safe: miss one place and import fails.
assert set(HANDLERS) == {i.value for i in Intent}, (
    "handler registry and Intent enum disagree: "
    f"{set(HANDLERS) ^ {i.value for i in Intent}}"
)


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("classify", classify)
    for name, fn in HANDLERS.items():
        g.add_node(name, fn)
    g.add_node("respond", handlers.respond)

    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route, {n: n for n in HANDLERS})
    for name in HANDLERS:
        g.add_edge(name, "respond")  # one place to finalise the trace
    g.add_edge("respond", END)
    return g.compile()


_GRAPH = None


def graph():
    """Compiled once, reused per request."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH
