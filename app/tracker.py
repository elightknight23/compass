"""The complaint tracker — every facility complaint, mirrored to GitHub Issues.

The facilities crew works from an issue tracker; students and the warden
work from Compass. Each complaint is also an issue in one repository
(`GITHUB_REPO`), kept in step with the record:

    issue title    "[CMP-1004] AC in the library reading room isn't working"
    labels         compass, category:library, priority:high
    state          open / closed — closed as `completed` when resolved and as
                   `not_planned` when cancelled, so the tracker can tell a
                   fix from a withdrawal (GitHub's own `state_reason`)
    body           the complaint, its status and its history, with who acted
                   by role, never by name

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

Off unless GITHUB_REPO and GITHUB_TOKEN are both set. Leave requests are
never mirrored: they are a student's academic record, not a work order.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone

import httpx

from . import backends, config
from .backends import COMPLAINT, COMPLAINT_PREFIX

LABEL = "compass"
CLOSED_AS = {"resolved": "completed", "cancelled": "not_planned"}
ROLE_WORDS = {"student": "the student", "hostel_warden": "the hostel warden",
              "faculty_advisor": "the faculty advisor", "hod": "the HOD"}
VERB_WORDS = {"file": "Filed", "amend": "Changed", "withdraw": "Withdrawn",
              "resolve": "Resolved", "cancel": "Cancelled"}

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
def desired(record: dict, events: list[dict]) -> dict:
    """The issue, as the record says it should be. Pure."""
    first = (record.get("notes") or "Facility complaint").strip().splitlines()[0]
    title = f"[{record['id']}] {first[:80]}{'…' if len(first) > 80 else ''}"
    roles = {u["id"]: u.get("role") for u in _directory()}
    history = [
        f"- {str(e.get('at') or '')[:10]} · {VERB_WORDS.get(e.get('verb'), str(e.get('verb')).title())}"
        f" by {ROLE_WORDS.get(roles.get(e.get('actor_id')), 'staff')}"
        + (f" — {e['note']}" if e.get("note") else "")
        for e in events
    ]
    body = "\n".join([
        record.get("notes") or "",
        "",
        "| | |", "|---|---|",
        f"| Compass record | `{record['id']}` |",
        f"| Status | **{record['status']}** |",
        f"| Category | {record.get('category') or '—'} |",
        f"| Priority | {record.get('priority') or '—'} |",
        f"| Filed | {record.get('created_at') or '—'} |",
        "",
        "**History**",
        *(history or ["- (none yet)"]),
        "",
        "<sub>Mirrored from Compass, which is where this complaint is decided. "
        "Changes made here are not copied back.</sub>",
    ])
    state = "open" if record["status"] == "open" else "closed"
    return {
        "title": title,
        "body": body,
        "labels": [LABEL, f"category:{record.get('category') or 'other'}",
                   f"priority:{record.get('priority') or 'medium'}"],
        "state": state,
        **({"state_reason": CLOSED_AS.get(record["status"], "completed")} if state == "closed" else {}),
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
        if record is None or record.get("type") != COMPLAINT:
            return backends.ok(None, outcome="not_mirrored")
        events = backend.list_events(record_id)
        want = desired(record, events["data"] if events["success"] else [])
        state = dict(record.get("tracker") or {})
        try:
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
    res = backends.get_backend().list_all_records((COMPLAINT,))
    rows = res["data"] or [] if res["success"] else []
    return [r["id"] for r in rows if not (r.get("tracker") or {}).get("synced")]


def sync_pending() -> dict:
    """Every complaint not yet in step: never mirrored, or the last sync failed."""
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
    """A record backend that schedules a sync after every complaint event.

    Every complaint change in the handlers ends in append_event (file,
    amend, withdraw, resolve, cancel), so that one hook covers chat and the
    console alike, and cannot be forgotten by a new handler. It adds no
    checks — backends stay dumb — it only notices.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.name = inner.name

    def __getattr__(self, attr):
        return getattr(self._inner, attr)

    def append_event(self, record_id: str, *args, **kwargs) -> dict:
        res = self._inner.append_event(record_id, *args, **kwargs)
        if enabled() and str(record_id).upper().startswith(COMPLAINT_PREFIX):
            # Out of step until the sync lands — recorded now, so a server
            # that dies before the thread runs leaves it for the retry loop.
            got = self._inner.get_record(record_id)
            if got["success"] and got["data"]:
                state = dict(got["data"].get("tracker") or {})
                self._inner.update_record(record_id, tracker={**state, "synced": False})
            schedule(record_id)
        return res
