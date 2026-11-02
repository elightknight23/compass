"""The tracker — every complaint and leave request, mirrored to GitHub Issues.

Staff can follow the desk from an issue tracker; students, the warden and
the advisor work from Compass. Each record is also an issue in one
repository (`GITHUB_REPO`), kept in step with it:

    issue title    "[CMP-1004] AC in the library reading room isn't working"
                   "[OD-2001] OD · Wed 25 Nov – Fri 27 Nov · 3 working days"
    labels         compass, type:complaint|leave, status:<status>, and
                   category: + priority: for a complaint, leave: (+
                   needs-exception, awaiting-certificate) for leave — so the
                   repository filters like a queue
    state          open while undecided; closed as `completed` when resolved
                   or approved, and as `not_planned` otherwise, so the
                   tracker can tell a fix from a withdrawal (GitHub's own
                   `state_reason`)
    body           the record, its status and its history, with who acted
                   by role, never by name

**A leave request is mirrored without its reason.** The reason is a
student's health or family business, and the certificate is the student's
and the advisor's alone (app/certificates.py); neither leaves Compass. The
issue says what kind of leave, when, for how many working days, and
whether it waits on an exception or a certificate — no names, no
percentages.

**Compass is the source of truth; the mirror is one-way.** Decisions are
made in Compass, where the role checks are (I1). Closing an issue on GitHub
changes nothing here — anyone with access to the repository could do that.

**The mirror never stands in the student's way.** A complaint is saved
first; the issue follows on a background thread. If GitHub is slow, down or
refusing the token, the record carries `tracker.synced = false` with the
reason, and a retry loop (and `scripts/sync_tracker.py`) catches it up. A
failed sync never invents an issue number.

**Sync is idempotent.** It computes what the issue should look like from the
record and its events and makes it so, so running it twice, or after a
missed update, converges. An issue created before a demo reset (same
Compass id) is found by its title and reused, not duplicated.

Off unless GITHUB_REPO and GITHUB_TOKEN are both set.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone

import httpx

from . import backends, config
from .backends import COMPLAINT, COMPLAINT_PREFIX, OD_PREFIX, OD_REQUEST

LABEL = "compass"
MIRRORED = (COMPLAINT, OD_REQUEST)
OPEN = ("open", "submitted")
DONE = ("resolved", "approved")  # closed as completed; anything else as not_planned
ROLE_WORDS = {"student": "the student", "hostel_warden": "the hostel warden",
              "faculty_advisor": "the faculty advisor", "hod": "the HOD"}
VERB_WORDS = {"file": "Filed", "submit": "Submitted", "amend": "Changed",
              "withdraw": "Withdrawn", "resolve": "Resolved", "cancel": "Cancelled",
              "approve": "Approved", "reject": "Rejected", "certificate": "Certificate"}
LEAVE_NAMES = {"od": "OD", "medical": "Medical leave", "family_emergency": "Family-emergency leave"}

# Colours and descriptions, so the repository's label list reads as a key.
_STATUS = {"open": "e3b341", "submitted": "e3b341", "resolved": "3fb950", "approved": "3fb950",
           "rejected": "cf222e", "withdrawn": "8c959f", "cancelled": "8c959f"}
LABEL_STYLE = {
    LABEL: ("1f6f5c", "Mirrored from Compass"),
    "type:complaint": ("d97757", "A facility complaint"),
    "type:leave": ("2f7d6d", "An OD or leave request (its reason stays in Compass)"),
    "needs-exception": ("fb8500", "Below the attendance threshold: needs the advisor's exception"),
    "awaiting-certificate": ("fbca04", "Medical leave with no certificate attached yet"),
    "priority:high": ("cf222e", ""), "priority:medium": ("e3b341", ""), "priority:low": ("8c959f", ""),
    "leave:od": ("6e7bd9", "On duty"), "leave:medical": ("d4a5c8", "Medical leave"),
    "leave:family-emergency": ("b08968", "Family-emergency leave"),
    **{f"status:{k}": (v, f"{k.title()} in Compass") for k, v in _STATUS.items()},
}

# Tests swap in an httpx.MockTransport, and run syncs inline.
_transport: httpx.BaseTransport | None = None
SYNC_INLINE = False
_LOCK = threading.Lock()


def _log(message: str) -> None:
    print(f"[compass-tracker] {message}", file=sys.stderr, flush=True)


def enabled() -> bool:
    return bool(config.GITHUB_REPO and config.GITHUB_TOKEN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# What the issue should say
# ---------------------------------------------------------------------------
def _day(iso: str | None) -> str:
    if not iso:
        return "—"
    d = datetime.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def desired(record: dict, events: list[dict]) -> dict:
    """The issue, as the record says it should be. Pure."""
    leave = record.get("type") == OD_REQUEST
    status = record["status"]
    roles = {u["id"]: u.get("role") for u in _directory()}
    history = [
        f"- {str(e.get('at') or '')[:10]} · {VERB_WORDS.get(e.get('verb'), str(e.get('verb')).title())}"
        f" by {ROLE_WORDS.get(roles.get(e.get('actor_id')), 'staff')}"
        + (f" — {e['note']}" if e.get("note") else "")
        for e in events
    ]
    if leave:
        kind = record.get("leave_kind") or "od"
        span = _day(record.get("start_date"))
        if record.get("end_date") and record.get("end_date") != record.get("start_date"):
            span += f" – {_day(record['end_date'])}"
        days = record.get("working_days")
        title = f"[{record['id']}] {LEAVE_NAMES.get(kind, kind)} · {span}" + (
            f" · {days} working day{'s' if days != 1 else ''}" if days is not None else "")
        rows = [
            f"| Kind | {LEAVE_NAMES.get(kind, kind)} |",
            f"| Dates | {span} |",
            f"| Working days | {days if days is not None else '—'} |",
            f"| Needs an attendance exception | {'yes' if record.get('needs_exception') else 'no'} |",
            *([f"| Certificate | {'attached' if record.get('certificate') else 'not yet'} |"]
              if kind == "medical" else []),
        ]
        intro = "A leave request. Its reason, and any certificate, stay in Compass."
        labels = [f"leave:{kind.replace('_', '-')}",
                  *(["needs-exception"] if record.get("needs_exception") and status in OPEN else []),
                  *(["awaiting-certificate"] if kind == "medical" and not record.get("certificate")
                    and status in OPEN else [])]
    else:
        first = (record.get("notes") or "Facility complaint").strip().splitlines()[0]
        title = f"[{record['id']}] {first[:80]}{'…' if len(first) > 80 else ''}"
        rows = [f"| Category | {record.get('category') or '—'} |",
                f"| Priority | {record.get('priority') or '—'} |"]
        intro = record.get("notes") or ""
        labels = [f"category:{record.get('category') or 'other'}",
                  f"priority:{record.get('priority') or 'medium'}"]
    body = "\n".join([
        intro,
        "",
        "| | |", "|---|---|",
        f"| Compass record | `{record['id']}` |",
        f"| Status | **{status}** |",
        *rows,
        f"| Filed | {record.get('created_at') or '—'} |",
        "",
        "**History**",
        *(history or ["- (none yet)"]),
        "",
        f"<sub>Mirrored from Compass, which is where this {'request' if leave else 'complaint'} "
        "is decided. Changes made here are not copied back.</sub>",
    ])
    state = "open" if status in OPEN else "closed"
    return {
        "title": title,
        "body": body,
        "labels": [LABEL, f"type:{'leave' if leave else 'complaint'}", f"status:{status}", *labels],
        "state": state,
        **({"state_reason": "completed" if status in DONE else "not_planned"} if state == "closed" else {}),
    }


def _directory() -> list[dict]:
    from . import users  # late: users reads through the backend
    return users.all_users()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class TrackerError(Exception):
    pass


def _call(method: str, path: str, **kwargs) -> dict | list:
    url = f"{config.GITHUB_API.rstrip('/')}/repos/{config.GITHUB_REPO}{path}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "Authorization": f"Bearer {config.GITHUB_TOKEN}"}
    try:
        with httpx.Client(timeout=config.GITHUB_TIMEOUT, transport=_transport) as client:
            r = client.request(method, url, headers=headers, **kwargs)
    except httpx.TimeoutException:
        raise TrackerError(f"GitHub didn't answer within {config.GITHUB_TIMEOUT:g}s") from None
    except httpx.HTTPError as e:
        raise TrackerError(f"couldn't reach GitHub ({type(e).__name__})") from None
    if r.status_code == 401:
        raise TrackerError("GitHub rejected the token")
    if r.status_code in (403, 429):
        limited = r.status_code == 429 or r.headers.get("x-ratelimit-remaining") == "0"
        raise TrackerError("GitHub is rate-limiting us" if limited else
                           "the token can't write issues in that repository")
    if r.status_code == 404:
        raise TrackerError(f"no repository {config.GITHUB_REPO} (or the token can't see it)")
    if r.status_code == 410:
        raise TrackerError("issues are turned off in that repository")
    if not r.is_success:
        raise TrackerError(f"GitHub answered {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise TrackerError("GitHub sent back something that wasn't JSON") from None


_labels_ready: set[str] = set()


def _ensure_labels(names: list[str]) -> None:
    """Give the labels this issue uses their colour and description, once.
    GitHub would create a missing one anyway, grey and unexplained."""
    missing = [n for n in names if n not in _labels_ready]
    if not missing:
        return
    if not _labels_ready:
        existing = _call("GET", "/labels", params={"per_page": 100})
        _labels_ready.update(str(label.get("name")) for label in existing)
        missing = [n for n in missing if n not in _labels_ready]
    for name in missing:
        color, description = LABEL_STYLE.get(name, ("c5d1dc", ""))
        try:
            _call("POST", "/labels", json={"name": name, "color": color, "description": description})
        except TrackerError:
            pass  # most likely it exists already; the issue still gets it
        _labels_ready.add(name)


def _find_existing(record_id: str) -> dict | None:
    """An issue this repository already has for this Compass id."""
    prefix = f"[{record_id}]"
    issues = _call("GET", "/issues", params={"labels": LABEL, "state": "all", "per_page": 100})
    return next((i for i in issues if str(i.get("title", "")).startswith(prefix)
                 and "pull_request" not in i), None)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def sync(record_id: str) -> dict:
    """Make the record's issue match the record. Returns the envelope of
    the record's new `tracker` state."""
    if not enabled():
        return backends.ok(None, outcome="disabled")
    with _LOCK:  # one sync at a time, so two can't both create the issue
        backend = backends.get_backend()
        got = backend.get_record(record_id)
        if not got["success"]:
            return got
        record = got["data"]
        if record is None or record.get("type") not in MIRRORED:
            return backends.ok(None, outcome="not_mirrored")
        events = backend.list_events(record_id)
        want = desired(record, events["data"] if events["success"] else [])
        state = dict(record.get("tracker") or {})
        try:
            _ensure_labels(want["labels"])
            number = state.get("issue")
            if number is None:
                found = _find_existing(record_id)
                if found:
                    number = found["number"]
                else:
                    made = _call("POST", "/issues", json={k: want[k] for k in ("title", "body", "labels")})
                    number = made["number"]
                    state.update(issue=number, url=made.get("html_url"))
            issue = _call("PATCH", f"/issues/{number}", json=want)
            state.update(issue=number, url=issue.get("html_url") or state.get("url"),
                         synced=True, error=None, synced_at=_now())
            outcome = "synced"
        except TrackerError as e:
            state.update(synced=False, error=str(e), tried_at=_now())
            outcome = "failed"
            _log(f"{record_id}: {e}")
        backend.update_record(record_id, tracker=state)
        return backends.ok(state, outcome=outcome)


def schedule(record_id: str) -> None:
    """Sync soon, off the request thread."""
    if not enabled():
        return
    if SYNC_INLINE:
        sync(record_id)
        return
    threading.Thread(target=sync, args=(record_id,), daemon=True,
                     name=f"tracker-{record_id}").start()


def pending() -> list[str]:
    res = backends.get_backend().list_all_records(MIRRORED)
    rows = res["data"] or [] if res["success"] else []
    return [r["id"] for r in rows if not (r.get("tracker") or {}).get("synced")]


def sync_pending() -> dict:
    """Every record not yet in step: never mirrored, or the last sync failed."""
    done = failed = 0
    for record_id in pending():
        res = sync(record_id)
        if res.get("outcome") == "synced":
            done += 1
        elif res.get("outcome") == "failed":
            failed += 1
    return {"synced": done, "failed": failed}


def start_retry_loop(stop: threading.Event | None = None) -> threading.Event:
    """Catch up at startup, then every TRACKER_RETRY_SECONDS."""
    stop = stop or threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                result = sync_pending()
                if result["synced"] or result["failed"]:
                    _log(f"retry pass: {result['synced']} synced, {result['failed']} still failing")
            except Exception as e:  # noqa: BLE001 — a background loop must not die
                _log(f"retry pass failed: {type(e).__name__}: {e}")
            stop.wait(config.TRACKER_RETRY_SECONDS)

    if enabled():
        threading.Thread(target=loop, daemon=True, name="tracker-retry").start()
    return stop


class Mirrored:
    """A record backend that schedules a sync after every record event.

    Every change in the handlers ends in append_event (file, submit, amend,
    withdraw, resolve, approve, reject, cancel, a certificate), so that one
    hook covers chat and the console alike, and cannot be forgotten by a new
    handler. It adds no checks — backends stay dumb — it only notices.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.name = inner.name

    def __getattr__(self, attr):
        return getattr(self._inner, attr)

    def append_event(self, record_id: str, *args, **kwargs) -> dict:
        res = self._inner.append_event(record_id, *args, **kwargs)
        if enabled() and str(record_id).upper().startswith((COMPLAINT_PREFIX, OD_PREFIX)):
            # Out of step until the sync lands — recorded now, so a server
            # that dies before the thread runs leaves it for the retry loop.
            got = self._inner.get_record(record_id)
            if got["success"] and got["data"]:
                state = dict(got["data"].get("tracker") or {})
                self._inner.update_record(record_id, tracker={**state, "synced": False})
            schedule(record_id)
        return res
