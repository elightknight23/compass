# The LangGraph flow

Both diagrams are read straight off `app/graph.py` and `app/handlers.py`.
Node names, edges and the confidence floor are the real ones, not a
simplification.

## 1. The graph

One classifier, nine handlers, one exit. The shape makes the argument:
**there is exactly one model call that decides anything, and it happens
before anything is allowed to touch a record.** Everything downstream of
`classify` is deterministic Python.

```mermaid
flowchart TD
    S([START]) --> CLS["<b>classify</b><br/>Gemini · primary → fallback model<br/>structured output · the only deciding LLM call"]

    subgraph GROUND ["answers from documents"]
        REG["regulation_question"]
    end

    subgraph OWN ["acts on your own records"]
        FILE["file_complaint"]
        LV["request_od_leave"]
        AMEND["amend_record"]
        CANCEL["cancel_record"]
        STATUS["check_status"]
    end

    subgraph OTHER ["acts on someone else's record"]
        SA["staff_action"]
    end

    subgraph SAFE ["no side effect, by design"]
        CLR["clarify"]
        OOS["out_of_scope"]
    end

    CLS -->|"route: the intent IS the node name"| REG
    CLS --> FILE
    CLS --> LV
    CLS --> AMEND
    CLS --> CANCEL
    CLS --> STATUS
    CLS --> SA
    CLS --> CLR
    CLS --> OOS

    LV --> ATT["<b>attendance.project</b><br/>condoned OD counts as present<br/>below 75% → flagged, never refused"]
    SA --> LOCK["<b>apply_staff_action</b><br/>student? → verb fits type? → capability?<br/>→ still open? → exception granted?"]
    AMEND --> OWNER["owner-only<br/>(staff go through the lock)"]
    CANCEL --> OWNER

    REG -.->|"retrieve k=4 · local MiniLM"| CHROMA[("ChromaDB<br/>regulation sections")]
    LOCK -.-> BE
    FILE -.-> BE
    OWNER -.-> BE
    STATUS -.-> BE
    ATT -.-> BE

    BE["<b>RecordBackend</b> protocol<br/>postgres · json"]

    REG --> RESP
    FILE --> RESP
    ATT --> RESP
    OWNER --> RESP
    STATUS --> RESP
    LOCK --> RESP
    CLR --> RESP
    OOS --> RESP

    RESP["<b>respond</b><br/>one place to finalise the trace"] --> E([END])

    classDef llm fill:#0f766e,stroke:#134e4a,stroke-width:2px,color:#fff
    classDef gate fill:#b91c1c,stroke:#7f1d1d,stroke-width:2px,color:#fff
    classDef py fill:#1d4ed8,stroke:#1e3a8a,stroke-width:2px,color:#fff
    classDef port fill:#b45309,stroke:#78350f,stroke-width:2px,color:#fff
    classDef store fill:#1e293b,stroke:#0f172a,color:#fff
    class CLS llm
    class LOCK,OWNER gate
    class ATT py
    class BE port
    class CHROMA store
```

**Teal** is the only place a model decides anything. **Red** is where
authorization is decided. **Blue** is arithmetic the model never sees.
**Amber** is the seam that lets the record store change without any handler
knowing.

(`regulation_question`, `clarify` and `out_of_scope` also call the model, to
*phrase* an answer. None of those calls can choose an action or touch a
record.)

Two structural guarantees worth saying out loud:

- `assert set(HANDLERS) == {i.value for i in Intent}` at import time. A new
  intent without a node fails at startup, not mid-demo.
- Every handler edges into `respond`, never straight to `END`. One node stamps
  the trace, so no path can return a reply that isn't explained.

## 2. Inside `classify`: the seam

The model is asked what it *heard*, never what the date *is* and never what
the attendance *becomes*. It reports parts. `app/dates.py` does the calendar,
holidays included, and `app/attendance.py` does the division.

```mermaid
flowchart TD
    MSG["user message + last 6 turns"] --> LLM["classifier<br/>Pydantic schema, 9 intents"]

    LLM --> PARTS["date <b>parts</b> the model heard<br/>anchor_weekday · week_offset · end_weekday<br/>duration_weekdays · day_offset · explicit_start_date"]
    LLM --> META["intent · confidence · reasoning · record_id<br/>target_person · category · priority · leave_kind<br/>action · grant_exception"]

    PARTS --> PY["<b>dates.resolve</b><br/>pure Python · skips weekends and college holidays"]
    PY --> REAL["real dates<br/>Mon 16 Nov .. Fri 20 Nov · 4 working days<br/>(Founders' Day skipped)"]

    META --> FLOOR{"confidence &lt; 0.45<br/>and not out_of_scope?"}
    FLOOR -->|yes| DOWN["intent = <b>clarify</b><br/>trace keeps downgraded_from"]
    FLOOR -->|no| KEEP["intent stands"]

    REAL --> OUT["state.entities + state.debug"]
    DOWN --> OUT
    KEEP --> OUT
    OUT --> ROUTE(["route to the handler"])

    classDef llm fill:#0f766e,stroke:#134e4a,stroke-width:2px,color:#fff
    classDef py fill:#1d4ed8,stroke:#1e3a8a,stroke-width:2px,color:#fff
    class LLM llm
    class PY py
```

Both halves land in the trace (`heard` and `resolved`), and `/api/chat`
streams the classification *before* the answer. The UI shows the seam rather
than asserting it.

The floor's exception matters: `out_of_scope` is never downgraded. A confident
refusal and an unconfident refusal are both refusals, and routing a low-score
"write my assignment" into `clarify` would make the assistant ask a follow-up
question about something it should simply decline.

`target_person` is a lookup hint, never an authorization input. Turning
"Ravi" into a student and then acting on his record are two separate steps,
and only the second is gated, by `apply_staff_action`.
