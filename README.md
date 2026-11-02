# Compass

### A campus service desk that knows what it isn't allowed to do

Students ask one chat window about the regulations, their syllabus and the
academic calendar, file hostel, lab, library and transport complaints, and
apply for OD or leave. Hostel wardens resolve complaints. Faculty advisors
decide leave and upload the College's calendar and syllabi.

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
| `document_question` | Answered from the College's documents (built-in regulations, uploaded syllabi and calendars) via RAG, with section-level citations, filtered to the asker's branch and semester |
| `calendar_question` | *When* is the next holiday, which exams are next week, is there college on Friday: Python over the confirmed calendar, never the model reading dates |
| `mess_menu` | What IFC A, B, C or D is serving: your home mess unless you name one, the day worked out in Python ("tomorrow", "Friday"), one meal or the whole day |
| `file_complaint` | Opens a facility complaint: hostel, lab, library or transport, mirrored to a GitHub issue when configured |
| `request_od_leave` | OD, medical or family-emergency leave (nothing else is granted). Resolves the dates in Python, counts working days against the holiday calendar, projects attendance, and submits; medical leave then asks for a certificate |
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
2. **The system informs; a human decides.** Three days of medical leave take
   Ravi to 73.9%, below 75%. The request is **created anyway**, flagged, and
   the advisor must explicitly grant an exception to approve it. An agent
   that silently refuses a legitimate request is worse than one that
   escalates it.

None of those numbers is written anywhere in the code. They fall out of the
formula against the seeded attendance.

### Uploaded documents: the calendar and the syllabus

The faculty advisor uploads PDFs (or Word, Markdown, text) on the
**Documents** page. Two kinds matter most:

- **Syllabus.** The normaliser knows two layouts. The NIT one is a page per
  course: `CS1305 3-0-0 (3)` with the title on the next line, books under
  *Learning Resources: Text Books*, and electives, minors and honours as
  their own groups after the last semester. The other is the Anna
  University / VTU one (`SEMESTER V`, `CS3591` or `21CS51`, `UNIT I`,
  `TEXT BOOKS`). Running headers, page footers and the CO-PO matrix are
  dropped. Either way, the chunk listing a course's books is headed
  *CSE Curriculum › Semester 5 › CS1305 Computer Networks › Text Books*.
  Tested on the department's 171-page curriculum: 112 courses. Every
  chunk carries its branch and semester. When Ravi (CSE, semester 5) asks for
  "the textbook for Computer Networks", Python builds the filter from his
  profile. An ECE student asking the same thing gets *"No syllabus for ECE has
  been uploaded yet"*, never CSE's books.
- **Academic calendar.** Dates are parsed by regex (day-first; ranges; a
  missing year taken from the academic year), **never by the model**. Every
  guess is flagged for the reviewer, and a weekday printed in the row that
  disagrees with the date catches a swapped day and month. The calendar lands
  as *needs review*: the advisor ticks, fixes and confirms each row. Only
  confirmed holidays change working days, per year of study, and they replace
  the placeholder `data/holidays.json` outright. "When is the next holiday?"
  is then answered by Python comparing those dates with today.

Students and wardens can't upload, and the server re-checks that on every
upload, delete and confirm (403, with nothing stored). An upload is read by
every student's assistant, so it's a write into everyone's answers. Uploaded
text gets the same injection defence as the regulations: the red-team script
plants an instruction inside a syllabus PDF and checks it's quoted, not
obeyed.

### Leave: what the College grants

Three kinds, and nothing else: **OD**, **medical leave** and **family
emergency**. The classifier reports what it heard. A family function or
a trip home comes back as `personal`, and Python refuses it, citing the
policy, without creating anything.

- **After the fact.** Medical and family-emergency leave can be applied for
  up to 7 days after the first day (`LEAVE_BACKDATE_DAYS`); OD cannot. Days
  already gone are already in the attendance figures as absences, so the
  projection adds only the days still to come.
- **The certificate.** Medical leave asks for one (PDF or a photo), right in
  the chat reply or on *My requests*. Only the student can attach it, while
  the request is undecided. Only the student and whoever decides the leave can
  open it. The warden, who can read every record, sees only "certificate
  attached", without even the filename. The advisor can't approve medical leave
  without one (409 `needs_certificate`). The certificate is read by a person,
  not a model: free-tier model inputs may be used by the provider, and a
  model's opinion that a document looks genuine is not evidence.

### Complaints and leave on GitHub Issues

Set `GITHUB_REPO` and a fine-grained `GITHUB_TOKEN` (Issues: read and write,
on one **private** repository) and every complaint and leave request is also
an issue there:

- **Labels, so the repository filters like a queue.** `type:complaint` or
  `type:leave`, `status:open|submitted|resolved|approved|rejected|…`, then
  `category:` and `priority:` for a complaint, or `leave:od|medical|family-emergency`
  plus `needs-exception` and `awaiting-certificate` while those hold. Labels are
  created with colours and descriptions.
- **State.** Open while undecided; closed as *completed* when resolved or
  approved, and *not planned* when rejected, withdrawn or cancelled.
- **Body.** A complaint's text, or a leave's kind, dates and working days, with
  its history by role (never by name). **A leave's reason and certificate never
  leave Compass**, and neither does any attendance figure.
- **Link back.** The record shows a link to its issue.

The mirror is **one-way**. Compass decides, where the role checks are; closing
the issue on GitHub changes nothing here. A complaint is saved first and the
issue follows on a background thread, so GitHub being slow or down never
stops a student. The record says it is out of step, and a retry loop
(startup, then every 5 minutes, or `scripts/sync_tracker.py`) catches it up
without duplicates.

### The mess menu

Each mess (IFC A, B, C, D) has a weekly menu: breakfast, lunch, snacks and
dinner for each day, with timings. Everyone can read every menu on the **Mess
menu** page, which opens on your own mess and today. Hostellers have a home
mess; a day scholar asking in chat is asked which mess they mean.

- **Only the hostel warden edits** (`auth.MENU_MANAGERS`, checked in
  `app/mess.py`, not just hidden on the page). Edit the grid, one dish per
  line, or **Import CSV** (`Day, Breakfast, Lunch, Snacks, Dinner`). An import
  fills the editor for checking and saves nothing until **Save menu**.
  **Download CSV** gives the current menu in that same shape, as a template.
- **Until a mess's menu is saved, a sample is shown**, labelled as one on the
  page and in every chat answer.

---

## Run it

Requires Python 3.12+ (developed on 3.14) and Node 18+. Docker is optional:
it's only needed for Postgres, and JSON works with no setup.

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
cp .env.example .env            # add GEMINI_API_KEY (aistudio.google.com)

.venv/bin/python scripts/index_docs.py          # build the index: regulations + any uploads (downloads MiniLM once, ~80 MB)
.venv/bin/python scripts/reset_demo.py          # start from the seed (keeps uploaded documents; --documents removes them)

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

Re-run `seed_db.py` after pulling a change to `app/models.py` (it drops and
recreates the tables; uploaded documents are kept with `reset_demo.py`).

If the database is unreachable during a demo, `RECORD_BACKEND=json` and a
restart is the whole recovery. Both backends satisfy the same protocol and are
tested against the same checks.

### Demo accounts

Published on purpose. These are fictional accounts with no real data behind them.

| Username | Password | Role |
| :--- | :--- | :--- |
| `ravi.kumar` | `ravi-demo` | Student, CSE sem 5, 76.2%, one day of margin, hosteller (IFC A) |
| `arjun.menon` | `arjun-demo` | Student, CSE sem 5, already below 75% |
| `priya.sharma` | `priya-demo` | Student, ECE sem 3, comfortable |
| `meena.krishnan` | `warden-demo` | Hostel warden: resolves complaints, keeps the mess menus |
| `lakshmi.narayanan` | `advisor-demo` | Faculty advisor: decides leave, uploads documents |

### The model: free, behind one interface

`app/llm.py` talks to **Google AI Studio's** free tier over its
OpenAI-compatible endpoint: `gemini-3.5-flash-lite` first, and
`gemini-3.1-flash-lite` on the same key when the first is rate-limited or
down (each model has its own free quota). The fallback is for availability
only (rate limits, outages, a rejected key, a response that isn't a chat
completion), never because an answer looked wrong. The trace records which
model answered.

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
             classify ── LLM, structured output, 11 intents     <- the only creative step
                       │
                       v
             dates.resolve ── Python calendar, college holidays
                       │
                       v
             handlers.py ── business rules + AUTHORIZATION     <- re-checks the role itself
                │        │            │
                v        v            v
             rag.py   attendance.py   RecordBackend (postgres | json)
               ^
     library.py ── upload · extract (documents.py) · index · calendar review
```

Diagrams: [`docs/diagrams/langgraph_flow.md`](docs/diagrams/langgraph_flow.md).

```
app/
  main.py          FastAPI: login, chat (SSE), records, queue, actions, reindex
  graph.py         the classifier and the LangGraph StateGraph
  handlers.py      one handler per intent, plus apply_staff_action
  state.py         AgentState and the Intent enum
  rag.py           chunking, local embeddings, filtered retrieval, grounded answers, index freshness
  documents.py     extraction (PDF tables, DOCX), syllabus structure, calendar date parsing — pure
  library.py       upload, ingest, delete, calendar review — re-checks the caller on every write
  mess.py          the mess menus: names, CSV, the sample, the warden's save — re-checks the caller
  dates.py         DateSpec + resolve() + working days against the holiday calendar
  attendance.py    the projection, and nothing else
  clock.py         the one definition of "today"
  auth.py          scrypt hashing, signed sessions, ROLE_CAPABILITIES
  llm.py           Gemini primary + fallback model, one pooled client
  warmup.py        pays cold-start cost on a background thread
  models.py        SQLAlchemy schema
  backends/        the swap boundary: postgres_backend, json_backend
docs/regulations/  the built-in corpus — re-index after editing
data/uploads/      uploaded originals + their extracted text (gitignored)
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
   resolves them. Calendar dates are parsed by regex and confirmed by a person;
   "the next holiday" is a comparison in Python. In the predecessor, the model asked for "Monday through
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
| that file is already uploaded / still processing | 409 |
| not a PDF, Word, Markdown or text file | 415 |
| over the size limit | 413 |

---

## Testing and evaluation

```bash
bash scripts/run_tests.sh       # 753 checks, no model, no network, ~2 min
```

| Suite | Checks | Exercises |
| :--- | ---: | :--- |
| `test_staff_lookup.py` | 61 | the full 3 roles × 2 types × 4 verbs matrix; every refusal leaves the record unmoved |
| `test_auth.py` | 70 | sessions, forged headers, scoped queues, refused POSTs asserted as 403 **and** an unchanged row, the SSE stream |
| `test_backends.py` | 139 | one protocol suite (records, documents, calendar events, mess menus) against JSON, SQLite and a real Postgres test database |
| `test_documents.py` | 167 | extraction from real PDFs/DOCX, both syllabus layouts, every calendar date shape (week grids too), adding and removing dates in review, the upload API by role, filtered retrieval, holidays in force |
| `test_attendance.py` | 60 | the projection: OD raises, medical lowers, threshold crossing, held=0; which leave is granted; leave already taken is not counted twice |
| `test_certificates.py` | 27 | who may attach and open a medical certificate, over HTTP; the warden's redacted copy; approval refused without one |
| `test_mess.py` | 64 | mess names, cleaning, CSV import/export, only the warden writes (over HTTP, refusals change nothing), the sample label, chat answers by day and meal |
| `test_tracker.py` | 41 | the GitHub mirror against a fake API: labels, close reasons, no names, GitHub down then back, no duplicates, leave without its reason |
| `test_dates.py` | 36 | calendar arithmetic against a pinned clock, the holiday calendar, blank classifier fields |
| `test_calendar.py` | 29 | `calendar_question` spans and answers per year of study; `document_question` filters and absence |
| `test_amend.py` | 25 | amend and cancel, ownership, "the most recent open one" |
| `test_llm.py` | 18 | the provider chain against a fake transport: 429 / 5xx / non-completion fall back, schema retry |
| `test_rag.py` | 16 | chunking, stale-index detection (content and format), the planted-injection guard |

Every suite runs against a throwaway copy of the seed data and blanks the
model key and the GitHub settings first, so none of them can touch the demo
store, the network or a real repository.

The harnesses that need a model, with outputs saved to `docs/artifacts/`:

| Harness | Produces |
| :--- | :--- |
| `run_acceptance.py` | 30 end-to-end rows through the real graph (uploaded syllabus and confirmed calendar included), plus a dead-database drill |
| `eval_intents.py --both` | accuracy and a confusion matrix over 62 labelled utterances, against a deliberately thin baseline prompt |
| `redteam_injection.py` | two planted injections: a circular in the regulations, a "system notice" in an uploaded syllabus |
| `compare_chunking.py` | naive versus structure-aware retrieval on a question built to straddle a heading |

How to read the intent score: report it as a **range** across runs.
On the 62-utterance set the shipping prompt scored 61/62 (31 Oct 2026) and
62/62 (1 Nov), the thin baseline 58/62; the saved run is in
`docs/artifacts/eval_matrix.txt`. Temperature 0 is not determinism. The eval set and the prompt share an
author, so the figure measures self-consistency, not resilience to
strangers.

---

## Known limitations

An unnamed limitation is the one that bites during questions.

- **The mess menu is one repeating week**, with a sample until the warden saves
  the real one. No rotating cycles, dated specials or holiday menus yet.

- **The built-in regulations and holiday list are placeholders**, written for
  a generic "College". Upload the real calendar and syllabi on the Documents
  page; replace `docs/regulations/` with the real rules and re-index.
- **No OCR.** A scanned PDF (no text layer) is refused with a reason, not
  indexed as nothing. Upload the original digital PDF or a Word file.
- **The parsers know two syllabus layouts** (NIT-style course pages, and
  Anna University / VTU-style) and day-first dates. Other layouts need the
  patterns in `app/documents.py` adjusted. Minor, honours and open-elective
  courses are filed under no semester, so a semester filter includes them.
- **Certificates are not verified.** Compass checks the file type, not
  whether the certificate is genuine; the advisor reads it.
- **The GitHub mirror is one-way**, and the issue body holds a complaint's
  text — use a private repository. Leave goes without its reason.
- **Saturday working days** ("Saturday working, Monday order") are listed but
  not added to the working-day count. Only holidays subtract.
- **Follow-ups lean on a 6-turn window.** "Yes, approve it as an exception"
  used to fail whenever the model didn't carry the record across the turn;
  now Python takes the id from Compass's own previous reply, but only for that
  explicit confirmation, and only when the reply named exactly one record.
- **Attendance is seeded, not synced.** There is no ERP integration. A CSV
  import is the honest interim.
- **One facility role.** The hostel warden resolves all facility complaints,
  including lab, library and transport. A real deployment would route each
  category to its own office.
- **Advisors are not scoped to their own sections.** Any faculty advisor can
  decide any student's leave and manage every branch's documents.
- **Free-tier model limits.** Long eval runs can spill from the primary
  model onto the fallback; the reports say which model answered. Both models
  are Google's, so a full Google outage takes chat down (the offline suites
  are unaffected).
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
