"""Medical certificates — attached by the student, read by the advisor.

The College grants medical leave on a certificate (od_and_leave_policy.md),
so an advisor cannot approve medical leave without one
(`handlers.apply_staff_action`, outcome `needs_certificate`). This module
is how one gets attached, and who may look at it.

A certificate is health information, which makes it the one record content
with a narrower read rule than the record itself:

- **Attach:** only the student whose leave it is, while the request is
  still undecided. Staff cannot attach one on a student's behalf.
- **Read:** the student, and whoever may *decide* the leave
  (`auth.may_action(user, "od_request")`). Being staff is not enough: a
  hostel warden may read every record (I2), but not this.

Nothing here is sent to a model. The advisor reads the certificate; a
model's opinion of whether a document is genuine is not evidence of it.

Same conventions as library.py: the caller is checked here, first, not
trusted from the endpoint; results are envelopes with an `outcome`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import auth, backends, config
from .backends import OD_REQUEST

# extension -> (magic bytes, media type). A photo from a phone, or a PDF.
TYPES = {
    ".pdf": (b"%PDF", "application/pdf"),
    ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
    ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
    ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
}


def _done(data=None, outcome: str = "ok") -> dict:
    return {"success": True, "data": data, "error": None, "outcome": outcome}


def _refused(error: str, outcome: str) -> dict:
    return {"success": False, "data": None, "error": error, "outcome": outcome}


def path_for(record: dict) -> Path | None:
    cert = record.get("certificate") or {}
    ext = cert.get("ext")
    return config.CERTIFICATES_DIR / f"{record['id']}{ext}" if ext else None


def may_read(user: dict | None, record: dict) -> bool:
    if not user:
        return False
    return record.get("user_id") == user["id"] or auth.may_action(user, OD_REQUEST)


def public(record: dict, user: dict | None) -> dict:
    """The record as this user may see it: someone who may not read the
    certificate learns only that one is attached — not even its filename,
    which is often a diagnosis ("dengue_report.pdf")."""
    cert = record.get("certificate")
    if not cert or may_read(user, record):
        return record
    return {**record, "certificate": {"attached": True}}


def _find(record_id: str) -> tuple[dict | None, dict | None]:
    res = backends.get_backend().get_record(record_id)
    if not res["success"]:
        return None, _refused(res["error"], "unavailable")
    if res["data"] is None:
        return None, _refused(f"No record {record_id} exists.", "not_found")
    return res["data"], None


def attach(user: dict | None, record_id: str, filename: str, data: bytes) -> dict:
    if not user:
        return _refused("Sign in to attach a certificate.", "not_signed_in")
    record, err = _find(record_id)
    if err:
        return err
    # Ownership first: whose leave it is decides who may attach, whatever
    # the role. Then the facts about the record, then the file.
    if record["user_id"] != user["id"]:
        return _refused(
            f"Only the student who applied for {record['id']} can attach its certificate.",
            "not_authorized",
        )
    if record.get("type") != OD_REQUEST or record.get("leave_kind") != "medical":
        return _refused(
            f"{record['id']} isn't medical leave, so it doesn't take a medical certificate.",
            "invalid",
        )
    if record.get("status") != "submitted":
        return _refused(
            f"{record['id']} is already **{record['status']}**, so its certificate "
            "can't be changed now.",
            "terminal_state",
        )

    ext = Path(filename or "").suffix.lower()
    if ext not in TYPES:
        return _refused("A certificate must be a PDF, JPG or PNG.", "unsupported_type")
    magic, media_type = TYPES[ext]
    if not data:
        return _refused("That file is empty.", "empty")
    if len(data) > config.MAX_CERTIFICATE_MB * 1024 * 1024:
        return _refused(f"That file is over {config.MAX_CERTIFICATE_MB} MB.", "too_large")
    if not data.startswith(magic):
        return _refused(f"That file isn't really a {ext[1:].upper()}.", "unsupported_type")

    config.CERTIFICATES_DIR.mkdir(parents=True, exist_ok=True)
    old = path_for(record)
    cert = {
        "filename": Path(filename).name[:200],
        "ext": ext,
        "content_type": media_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    target = config.CERTIFICATES_DIR / f"{record['id']}{ext}"
    tmp = target.with_suffix(ext + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    backend = backends.get_backend()
    res = backend.update_record(record["id"], certificate=cert)
    if not res["success"]:
        target.unlink(missing_ok=True)
        return _refused(f"The certificate couldn't be saved ({res['error']}).", "unavailable")
    if old and old != target:
        old.unlink(missing_ok=True)  # a PDF replacing a photo
    backend.append_event(
        record["id"], user["id"], "certificate", record["status"], record["status"],
        note="certificate replaced" if old else "certificate attached",
    )
    return _done(res["data"], "attached")


def open_file(user: dict | None, record_id: str) -> dict:
    """(path, media type, filename) for someone allowed to see it."""
    if not user:
        return _refused("Sign in to view a certificate.", "not_signed_in")
    record, err = _find(record_id)
    if err:
        return err
    if not may_read(user, record):
        return _refused(
            "A medical certificate is seen only by the student and by the faculty "
            "advisor who decides the leave.",
            "not_authorized",
        )
    path = path_for(record)
    if not path or not path.exists():
        return _refused(f"{record['id']} has no certificate attached.", "not_found")
    cert = record["certificate"]
    return _done({"path": path, "content_type": cert["content_type"], "filename": cert["filename"]})
