"""AgentState — the typed dict every LangGraph node reads and writes.

One state object flows classify -> route -> act -> respond. Nodes return
partial dicts and LangGraph merges them in, which matters for `debug`:
by default a returned key *replaces* the old value, so the handler's
trace would silently wipe the classifier's. `debug` therefore carries a
reducer (dict union) and every node contributes to the same trace
instead of fighting over it.

The trace is the reasoning panel in the UI — intent, confidence, what the
classifier heard and what Python resolved, retrieved chunks with sources,
the RBAC verdict, which backend and which model answered. It has to
accumulate across nodes, or the panel only ever shows the last one.
"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Any, TypedDict


class Intent(StrEnum):
    """The routing vocabulary — the single source of truth.

    The classifier's Pydantic schema, the graph's conditional edge, and
    the handler registry all read from this enum, so a new intent can't
    be added in one place and forgotten in the other two. graph.py asserts
    at import time that the handler registry and this enum agree.

    Small talk ("hi", "who are you") folds into OUT_OF_SCOPE with a
    friendly redirect rather than earning its own intent.
    """

    # What the College's documents SAY — a rule, a syllabus, the calendar's
    # text — answered from retrieved excerpts with citations.
    DOCUMENT_QUESTION = "document_question"
    # WHEN something dated happens: holidays, exams, semester dates, or
    # whether a given day is a working day. Answered by Python from the
    # confirmed calendar, not by the model reading dates out of excerpts:
    # "the next holiday" compares dates to today, which is arithmetic (I5).
    CALENDAR_QUESTION = "calendar_question"
    # What a mess serves, by day and meal. A lookup by weekday, so Python
    # resolves "today" or "Friday" and the model only says what it heard.
    MESS_MENU = "mess_menu"
    FILE_COMPLAINT = "file_complaint"
    # Changing a request the student already has. Split out of
    # FILE_COMPLAINT because without it "make it urgent", said one turn
    # after filing, is indistinguishable from reporting a new problem — and
    # the predecessor cheerfully filed a second ticket whose entire
    # description was "priority high please".
    AMEND_RECORD = "amend_record"
    # Withdrawing your OWN record. Separate from STAFF_ACTION because
    # "cancel my complaint, the fan works now" is not a staff decision.
    CANCEL_RECORD = "cancel_record"
    CHECK_STATUS = "check_status"
    REQUEST_OD_LEAVE = "request_od_leave"
    STAFF_ACTION = "staff_action"
    CLARIFY = "clarify"  # the designed safe failure: ask, don't guess
    OUT_OF_SCOPE = "out_of_scope"


class AgentState(TypedDict):
    """State passed between nodes. Every field is initialised by
    new_state() so no node has to defend against a missing key."""

    # -- inputs, set by FastAPI before invoke ------------------------------
    history: list[dict]  # sliding window from the UI: [{role, content}, ...]
    current_message: str
    user: dict | None  # resolved from the signed session cookie, never from text

    # -- filled by the classifier -----------------------------------------
    intent: str  # an Intent value; str keeps the trace JSON-serialisable
    confidence: float  # low confidence can route to CLARIFY
    entities: dict[str, Any]  # e.g. {"record_id": "CMP-1001"}

    # -- accumulated across nodes, rendered in the UI's Details panel ------
    debug: Annotated[dict[str, Any], operator.or_]

    # -- output ------------------------------------------------------------
    reply: str


def new_state(
    message: str, user: dict | None, history: list[dict] | None = None
) -> AgentState:
    """Build a fully-populated state for one turn."""
    return AgentState(
        history=history or [],
        current_message=message,
        user=user,
        intent="",
        confidence=0.0,
        entities={},
        debug={"user_id": (user or {}).get("id"), "role": (user or {}).get("role")},
        reply="",
    )
