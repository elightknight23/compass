"""Generate data/credentials.json from the demo account list.

Hashes are salted and non-deterministic, so this file is the reproducible
source rather than the hashes themselves — rotating a demo password means
editing ACCOUNTS and re-running, not pasting a base64 blob.

    python scripts/seed_credentials.py           # write
    python scripts/seed_credentials.py --check   # verify each login works

The passwords are published in the repo on purpose. These are fictional
accounts on a demo with no real data behind it, and a project nobody can
log into is not a demo. (Postgres gets the same hashes: scripts/seed_db.py
reads this file.) What matters is that the *storage* is honest: salted
scrypt, verified in constant time, never a plaintext column.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auth, config  # noqa: E402

# username -> (user_id, demo password)
ACCOUNTS = {
    "ravi.kumar": ("U001", "ravi-demo"),
    "priya.sharma": ("U002", "priya-demo"),
    "arjun.menon": ("U003", "arjun-demo"),
    "meena.krishnan": ("U101", "warden-demo"),
    "lakshmi.narayanan": ("U102", "advisor-demo"),
}

OUT = config.DATA_DIR / "credentials.json"


def write() -> int:
    payload = {
        username: {"user_id": uid, "password": auth.hash_password(pw)}
        for username, (uid, pw) in ACCOUNTS.items()
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} — {len(payload)} accounts")
    for username, (uid, pw) in ACCOUNTS.items():
        print(f"  {username:<18} {uid}  password: {pw}")
    return 0


def check() -> int:
    failures = 0
    for username, (uid, pw) in ACCOUNTS.items():
        user = auth.authenticate(username, pw)
        good = user is not None and user["id"] == uid
        wrong = auth.authenticate(username, pw + "x") is None
        print(f"{'ok  ' if good and wrong else 'FAIL'} {username} -> {uid}")
        failures += 0 if (good and wrong) else 1
    unknown = auth.authenticate("nobody", "whatever") is None
    print(f"{'ok  ' if unknown else 'FAIL'} unknown username is rejected")
    failures += 0 if unknown else 1
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify, don't rewrite")
    raise SystemExit(check() if ap.parse_args().check else write())
