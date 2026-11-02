"""Bring every complaint's and leave request's GitHub issue into step with Compass.

The server already does this at startup and every TRACKER_RETRY_SECONDS;
this is the same pass on demand — after fixing a token, say.

    python scripts/sync_tracker.py           # records not yet in step
    python scripts/sync_tracker.py --all     # every record, re-sent
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backends, tracker  # noqa: E402
from app.tracker import MIRRORED  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="re-send every record")
    args = ap.parse_args()
    if not tracker.enabled():
        print("tracker: off — set GITHUB_REPO and GITHUB_TOKEN in .env (see .env.example)")
        return 1
    res = backends.get_backend().list_all_records(MIRRORED)
    if not res["success"]:
        print(f"records: unreachable ({res['error']})")
        return 1
    ids = [r["id"] for r in res["data"]] if args.all else tracker.pending()
    failed = 0
    for record_id in ids:
        out = tracker.sync(record_id)
        state = out["data"] or {}
        if out.get("outcome") == "synced":
            print(f"{record_id}: issue #{state['issue']}  {state.get('url') or ''}")
        else:
            failed += 1
            print(f"{record_id}: FAILED — {state.get('error')}")
    print(f"{len(ids) - failed}/{len(ids)} in step with {tracker.config.GITHUB_REPO}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
