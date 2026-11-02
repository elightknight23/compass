"""Intent classifier evaluation — accuracy and a confusion matrix.

Isolates the classifier node: one structured-output call per utterance,
prediction compared against the label in data/eval_utterances.json. No
handlers run and nothing is written. Rows may carry a short `history`, for
follow-ups that only mean something after a previous turn.

    python scripts/eval_intents.py            # the shipping prompt
    python scripts/eval_intents.py --naive    # a deliberately thin prompt
    python scripts/eval_intents.py --both     # both, with the delta

--naive replaces the classifier rules with a bare list of intent names —
no disambiguation rules, no examples, no instruction to prefer clarify.
That is the "before" number, and the delta is the only honest way to claim
the prompt work did something.

How to report the result, and why:

- **As a range, not a number.** Temperature 0 is not determinism: in the
  predecessor, two runs four days apart scored differently with no code
  change, on a borderline utterance. Run it more than once; report the spread.
- **With its model.** On a free tier the primary model spills onto the
  fallback model when rate-limited, and a score from the fallback is a
  different measurement. The report prints which model answered each call.
- **With the conflict of interest.** The same person wrote these
  utterances and the prompt they are scored against. A high score measures
  self-consistency, not resilience to strangers.

This makes one model call per utterance (two with --both). The free tier
limits requests PER MINUTE, so calls are paced (--pace seconds apart) and a
row the model never answered is retried after a pause. A row that still
gets no answer is reported as NOT SCORED, never counted as a miss: an
unanswered call degrades to clarify, and scoring that would measure the
quota, not the prompt.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, graph, llm  # noqa: E402
from app.state import new_state  # noqa: E402

DATASET = config.DATA_DIR / "eval_utterances.json"
RETRIES = 2
RETRY_WAIT = 40  # seconds: long enough for a per-minute quota to refill

CODE = {
    "document_question": "DOC",
    "calendar_question": "CAL",
    "mess_menu": "MES",
    "file_complaint": "CMP",
    "request_od_leave": "LV ",
    "check_status": "STA",
    "amend_record": "AMD",
    "cancel_record": "CNL",
    "staff_action": "STF",
    "clarify": "CLR",
    "out_of_scope": "OOS",
}
ORDER = list(CODE)

NAIVE_RULES = """You are the intent classifier for a campus service desk.
Classify the user's message into exactly one intent.

document_question, calendar_question, mess_menu, file_complaint, request_od_leave,
check_status, amend_record, cancel_record, staff_action, clarify, out_of_scope"""

# The persona every utterance is classified as. A student, because that is
# who sends most of these; the classifier is told to classify what is ASKED
# regardless of role, so staff_action rows still expect staff_action.
ASKER = {"id": "U001", "name": "Ravi Kumar", "role": "student",
         "department": "Computer Science and Engineering"}


def load() -> list[dict]:
    return json.loads(DATASET.read_text())


def predict(row: dict) -> dict:
    for attempt in range(RETRIES + 1):
        state = new_state(row["utterance"], ASKER, row.get("history"))
        out = graph.classify(state)
        d = out["debug"]
        if not d.get("classifier_error") or attempt == RETRIES:
            break
        print(f"   (model unavailable on {row['utterance'][:40]!r}; retrying in {RETRY_WAIT}s)",
              file=sys.stderr, flush=True)
        time.sleep(RETRY_WAIT)
    return {
        **row,
        "got": out["intent"],
        "raw": d.get("downgraded_from") or out["intent"],
        "confidence": d.get("confidence"),
        "provider": d.get("provider") or "none",
        "error": d.get("classifier_error"),
    }


def run(rows: list[dict], label: str, naive: bool, pace: float) -> list[dict]:
    saved = graph.CLASSIFIER_RULES
    graph.CLASSIFIER_RULES = NAIVE_RULES if naive else saved
    out = []
    try:
        for i, row in enumerate(rows):
            if i:
                time.sleep(pace)  # one at a time, under the per-minute quota
            out.append(predict(row))
    finally:
        graph.CLASSIFIER_RULES = saved
    return out


def matrix(results: list[dict]) -> None:
    grid: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        if r["error"]:
            continue
        grid[r["expected"]][r["got"]] += 1
    print("\n  expected \\ got  " + " ".join(CODE[i] for i in ORDER))
    for exp in ORDER:
        cells = " ".join(
            (f"{grid[exp][g]:>3}" if grid[exp][g] else "  .") for g in ORDER
        )
        print(f"  {CODE[exp]:<15} {cells}")


def report(results: list[dict], label: str) -> tuple[int, int]:
    scored = [r for r in results if not r["error"]]
    correct = sum(r["got"] == r["expected"] for r in scored)
    providers = Counter(r["provider"] for r in scored)
    print(f"\n=== {label}: {correct}/{len(scored)} "
          f"(providers: {', '.join(f'{p}={n}' for p, n in providers.items()) or 'none'})")
    errors = [r for r in results if r["error"]]
    if errors:
        print(f"  {len(errors)} NOT SCORED — the model never answered "
              f"(e.g. {errors[0]['error'][:100]}). Re-run before quoting this.")
    matrix(results)
    misses = [r for r in scored if r["got"] != r["expected"]]
    if misses:
        print("\n  misses:")
        for r in misses:
            down = f" (downgraded from {r['raw']})" if r["raw"] != r["got"] else ""
            print(f"   - {r['utterance']!r}\n       expected {r['expected']}, "
                  f"got {r['got']}{down}  conf={r['confidence']}  [{r['trap']}]")
    return correct, len(scored)


def main() -> int:
    if not llm.providers():
        print("No model provider configured — set GEMINI_API_KEY in .env.")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--naive", action="store_true", help="thin prompt only")
    ap.add_argument("--both", action="store_true", help="naive then shipping, with the delta")
    ap.add_argument("--pace", type=float, default=4.0,
                    help="seconds between calls (free tier: ~15 requests/minute)")
    args = ap.parse_args()

    rows = load()
    print(f"{len(rows)} labelled utterances from {DATASET.relative_to(ROOT)}")
    if args.naive or args.both:
        naive, n_naive = report(run(rows, "naive", naive=True, pace=args.pace), "thin baseline prompt")
    if not args.naive:
        shipping, n_ship = report(run(rows, "shipping", naive=False, pace=args.pace), "shipping prompt")
    if args.both:
        if n_naive == n_ship == len(rows):
            print(f"\ndelta: {naive}/{len(rows)} -> {shipping}/{len(rows)} "
                  f"({shipping - naive:+d}). Report it as a range across runs.")
        else:
            print(f"\nno delta: not every row was scored ({n_naive} and {n_ship} of "
                  f"{len(rows)}). Re-run when the quota has recovered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
