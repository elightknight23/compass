# Compass

### A campus service desk that knows what it isn't allowed to do

Students ask one chat window about the regulations, file hostel, lab, library
and transport complaints, and apply for OD or leave. Hostel wardens resolve
complaints. Faculty advisors decide leave.

**The language model decides what was asked. Python decides what happens.**

The model is consulted once per turn, to classify intent and report the
parameters it *heard*. Everything after that is ordinary, testable Python:
authorization, calendar arithmetic, the attendance projection, and every write.

The sentence that defines the project is *"Approve Ravi's OD request"*. It
produces three different outcomes depending only on who is signed in:

| Signed in as | Outcome | Decided by |
| :--- | :--- | :--- |
| Ravi (student) | refused — students hold no capability | `if not auth.is_staff(user)` |
| Meena (hostel warden) | refused — staff, but leave isn't hers to decide; the reply names the advisor | `if not auth.may_action(user, "od_request")` |
| Dr. Lakshmi (faculty advisor) | approved, and logged against her account | both checks pass |

The thing that decides is an `if` statement, not a politely worded
instruction to a language model.

---

## What it does

| Intent | What happens |
| :--- | :--- |
| `regulation_question` | Answered from `docs/regulations/` via RAG, with section-level citations |
| `file_complaint` | Opens a facility complaint: hostel, lab, library or transport |
| `request_od_leave` | Resolves the dates in Python, counts working days against the holiday calendar, projects attendance, and submits |
| `check_status` | Your records and attendance always; anyone's if you are staff |
| `amend_record` | Changes priority or category of your own complaint, or the reason on your own leave request |
| `cancel_record` | Withdraws your own complaint or leave request |
| `staff_action` | approve / reject / resolve / cancel, staff only, within remit |
| `clarify` | Asks instead of guessing. A designed outcome, counted as a success |
| `out_of_scope` | Greetings, small talk, everything else |

### The attendance rule

This replaces the notice-period check in the predecessor, and it is the main
piece of new logic (`app/attendance.py`):

```
current   = attended / held
added     = working_days × periods_per_day          (6)
present   = added if the leave is OD else 0         (OD is condoned)
projected = (attended + present) / (held + added)
```

Two consequences are the point:

1. **OD can raise a percentage.** Condoned days count as present. Ravi is at
   76.2%; three days of OD take him to 76.9%.
2. **The system informs; a human decides.** Three days of casual leave take
   Ravi to 73.9%, below 75%. The request is **created anyway**, flagged, and
   the advisor must explicitly grant an exception to approve it. An agent
   that silently refuses a legitimate request is worse than one that
   escalates it.

None of those numbers is written anywhere in the code. They fall out of the
formula against the seeded attendance.

---

## Run it

Requires Python 3.12+ (developed on 3.14) and Node 18+. Docker is optional:
it's only needed for Postgres, and JSON works with no setup.

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
cp .env.example .env            # add GITHUB_MODELS_TOKEN and/or GEMINI_API_KEY

.venv/bin/python scripts/index_docs.py          # build the regulations index (downloads MiniLM once, ~80 MB)
.venv/bin/python scripts/reset_demo.py          # start from the seed

cd web && npm install && npm run build && cd ..
.venv/bin/python -m uvicorn app.main:app --port 8000
# open http://localhost:8000
```

For frontend development, run `npm run dev` in `web/` and open
http://localhost:5173 instead. Vite proxies `/api` to uvicorn.

**Postgres** (the system of record; JSON is the fallback):

```bash
docker compose up -d                            # Postgres 16 on host port 5433
.venv/bin/python scripts/seed_db.py
RECORD_BACKEND=postgres .venv/bin/python -m uvicorn app.main:app --port 8000
```

If the database is unreachable during a demo, `RECORD_BACKEND=json` and a
restart is the whole recovery. Both backends satisfy the same protocol and are
tested against the same checks.

### Demo accounts

Published on purpose. These are fictional accounts with no real data behind them.

| Username | Password | Role |
| :--- | :--- | :--- |
| `ravi.kumar` | `ravi-demo` | Student, 76.2%, one day of margin |
| `arjun.menon` | `arjun-demo` | Student, already below 75% |
| `priya.sharma` | `priya-demo` | Student, comfortable |
| `meena.krishnan` | `warden-demo` | Hostel warden: resolves complaints |
| `lakshmi.narayanan` | `advisor-demo` | Faculty advisor: decides leave |

### The model: free, behind one interface

`app/llm.py` talks to **GitHub Models** (`openai/gpt-4o-mini`) first and
**Google AI Studio** (`gemini-2.5-flash`) second, both over the OpenAI wire
format. The fallback is for availability only (rate limits, outages, a
rejected key), never because an answer looked wrong. The trace records which
provider answered.

Embeddings are **local**: all-MiniLM-L6-v2 on the ONNX runtime that ChromaDB
ships. That means no hosted embedder, no rate limit, no network in retrieval,
and no PyTorch.

---

## Architecture

```
Student chat ─┐
              ├─> FastAPI ── identity from a signed session cookie, nowhere else
Staff console ┘        │
                       v
             classify ── LLM, structured output, 9 intents      <- the only creative step
                       │
                       v
             dates.resolve ── Python calendar, college holidays
                       │
                       v
             handlers.py ── business rules + AUTHORIZATION     <- re-checks the role itself
                │        │            │
                v        v            v
             rag.py   attendance.py   RecordBackend (postgres | json)
```

Diagrams: [`docs/diagrams/langgraph_flow.md`](docs/diagrams/langgraph_flow.md).

```
app/
  main.py          FastAPI: login, chat (SSE), records, queue, actions, reindex
  graph.py         the classifier and the LangGraph StateGraph
  handlers.py      one handler per intent, plus apply_staff_action
  state.py         AgentState and the Intent enum
  rag.py           chunking, local embeddings, retrieval, grounded answers, index freshness
  dates.py         DateSpec + resolve() + working days against the holiday calendar
  attendance.py    the projection, and nothing else
  clock.py         the one definition of "today"
  auth.py          scrypt hashing, signed sessions, ROLE_CAPABILITIES
  llm.py           GitHub Models with Gemini fallback, one pooled client
  warmup.py        pays cold-start cost on a background thread
  models.py        SQLAlchemy schema
  backends/        the swap boundary: postgres_backend, json_backend
docs/regulations/  the knowledge corpus — re-index after editing
data/              seed data: users, credentials, attendance, holidays, records
scripts/           tests, evaluation harnesses, indexing, seeding, reset
web/               React + Vite + TypeScript
```

### The invariants

1. **Authorization is enforced in Python, at the point of the write.** Never
   in a prompt, never only in the UI. `apply_staff_action` re-checks the role
   itself, whichever door called it.
2. **Two questions, never one.** `is_staff` gates *reading* other people's
   records. `may_action(user, record_type)` gates *every write*, and needs the
   record in hand.
3. **The order of checks is deliberate:** students first, then the verb
   against the record type, then the caller's capability. Checking capability
   first would make the type check unreachable for every real role. A test
   pins this order (verified by mutation: swapping the two fails five checks).
4. **Retrieved text is data, never instructions.** The corpus contains a
   planted circular telling AI assistants the attendance requirement is
   waived. It *is* retrieved into context, and the answer still states the
   real rule.
5. **The model never does arithmetic.** It reports date parts; Python
   resolves them. In the predecessor, the model asked for "Monday through
   Friday, four weeks from now" returned Saturday to Wednesday, and the record
   was created anyway.
6. **Absence is a successful query with an empty answer.** `get_record` on a
   missing id is `ok(None)`, not an error.
7. **`clarify` is a designed outcome.** A staff action with no verb asks; it
   does not assume "approve".
8. **One definition of "today"** (`app/clock.py`). `COMPASS_TODAY` pins it
   for tests and warns on stderr whenever set.

Refusals stay distinguishable, in chat and over HTTP:

| Situation | HTTP |
| :--- | :--- |
| you may not | 403 |
| no such record | 404 |
| a complaint can't be approved | 400 |
| that record is already decided | 409 |
| approving it needs an attendance exception | 409 |

---

## Testing and evaluation

```bash
bash scripts/run_tests.sh       # 310 checks, no model, no network, ~40 s
```

| Suite | Checks | Exercises |
| :--- | ---: | :--- |
| `test_staff_lookup.py` | 57 | the full 3 roles × 2 types × 4 verbs matrix; every refusal leaves the record unmoved |
| `test_auth.py` | 63 | sessions, forged headers, scoped queues, refused POSTs asserted as 403 **and** an unchanged row, the SSE stream |
| `test_backends.py` | 72 | one protocol suite against JSON, SQLite and a real Postgres test database |
| `test_attendance.py` | 45 | the projection: OD raises, casual lowers, threshold crossing, held=0, and the handler end to end |
| `test_dates.py` | 33 | calendar arithmetic against a pinned clock, including the holiday calendar |
| `test_amend.py` | 25 | amend and cancel, ownership, "the most recent open one" |
| `test_rag.py` | 15 | chunking, stale-index detection, and a guard that keeps the planted injection in the corpus |

Every suite runs against a throwaway copy of the seed data and blanks the
model keys first, so none of them can touch the demo store or the network.

The harnesses that need a model, with outputs saved to `docs/artifacts/`:

| Harness | Produces |
| :--- | :--- |
| `run_acceptance.py` | 20 end-to-end rows through the real graph, plus a dead-database drill |
| `eval_intents.py --both` | accuracy and a confusion matrix over 45 labelled utterances, against a deliberately thin baseline prompt |
| `redteam_injection.py` | the planted-injection transcript |
| `compare_chunking.py` | naive versus structure-aware retrieval on a question built to straddle a heading |

How to read the intent score: report it as a **range** across runs.
Temperature 0 is not determinism. The eval set and the prompt share an
author, so the figure measures self-consistency, not resilience to
strangers.

---

## Known limitations

An unnamed limitation is the one that bites during questions.

- **The regulations and holiday calendar are placeholders.** They are written
  for a generic "College". Replace `docs/regulations/` and
  `data/holidays.json` with the real ones, then re-index.
- **Attendance is seeded, not synced.** There is no ERP integration. A CSV
  import is the honest interim.
- **One facility role.** The hostel warden resolves all facility complaints,
  including lab, library and transport. A real deployment would route each
  category to its own office.
- **Advisors are not scoped to their own sections.** Any faculty advisor can
  decide any student's leave.
- **Free-tier model limits.** GitHub Models' daily cap is low, so long eval
  runs spill onto the Gemini fallback. The reports say so.
- **Demo passwords are seeded and published.** There is no rotation or
  lockout, and cookies are not `Secure` (the demo runs on http://localhost).
  Harden before anything real.
- **A status change and its audit event are two writes**, not one
  transaction, even on Postgres.
- **Sliding-window conversation memory** (the last 6 turns from the browser),
  not checkpointed sessions.
- **The intent eval set and the prompt share an author.**

## Provenance

Compass builds on **Atlas**, the author's earlier agentic service-desk project.
Atlas was a workplace assistant with the same architecture: one classifier,
nine handlers, authorization in Python, and RAG with an injection defence.
The domain-agnostic core (clock, date parts, graph structure, the result
envelope, the injection defence, the session auth) was ported. The domain is
new: the corpus, roles, record types, attendance projection, holiday
calendar, Postgres backend, React frontend and test suites. The port also
fixed two authorization bugs it uncovered (see the `agent:` commit).
