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

This makes one model call per utterance (two with --both) — mind the
free-tier daily cap.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, graph, llm  # noqa: E402
from app.state import new_state  # noqa: E402

DATASET = config.DATA_DIR / "eval_utterances.json"
WORKERS = 3  # gentle on free-tier rate limits

CODE = {
    "document_question": "DOC",
    "calendar_question": "CAL",
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

document_question, calendar_question, file_complaint, request_od_leave,
check_status, amend_record, cancel_record, staff_action, clarify, out_of_scope"""

# The persona every utterance is classified as. A student, because that is
# who sends most of these; the classifier is told to classify what is ASKED
# regardless of role, so staff_action rows still expect staff_action.
ASKER = {"id": "U001", "name": "Ravi Kumar", "role": "student",
         "department": "Computer Science and Engineering"}


def load() -> list[dict]:
    return json.loads(DATASET.read_text())


def predict(row: dict) -> dict:
    state = new_state(row["utterance"], ASKER, row.get("history"))
    out = graph.classify(state)
    d = out["debug"]
    return {
        **row,
        "got": out["intent"],
        "raw": d.get("downgraded_from") or out["intent"],
        "confidence": d.get("confidence"),
        "provider": d.get("provider") or "none",
        "error": d.get("classifier_error"),
    }


def run(rows: list[dict], label: str, naive: bool) -> list[dict]:
    saved = graph.CLASSIFIER_RULES
    graph.CLASSIFIER_RULES = NAIVE_RULES if naive else saved
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            return list(pool.map(predict, rows))
    finally:
        graph.CLASSIFIER_RULES = saved


def matrix(results: list[dict]) -> None:
    grid: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        grid[r["expected"]][r["got"]] += 1
    print("\n  expected \\ got  " + " ".join(CODE[i] for i in ORDER))
    for exp in ORDER:
        cells = " ".join(
            (f"{grid[exp][g]:>3}" if grid[exp][g] else "  .") for g in ORDER
        )
        print(f"  {CODE[exp]:<15} {cells}")


def report(results: list[dict], label: str) -> int:
    correct = sum(r["got"] == r["expected"] for r in results)
    providers = Counter(r["provider"] for r in results)
    print(f"\n=== {label}: {correct}/{len(results)} "
          f"(providers: {', '.join(f'{p}={n}' for p, n in providers.items())})")
    errors = [r for r in results if r["error"]]
    if errors:
        print(f"  {len(errors)} classifier errors, e.g. {errors[0]['error'][:120]}")
    matrix(results)
    misses = [r for r in results if r["got"] != r["expected"]]
    if misses:
        print("\n  misses:")
        for r in misses:
            down = f" (downgraded from {r['raw']})" if r["raw"] != r["got"] else ""
            print(f"   - {r['utterance']!r}\n       expected {r['expected']}, "
                  f"got {r['got']}{down}  conf={r['confidence']}  [{r['trap']}]")
    return correct


def main() -> int:
    if not llm.providers():
        print("No model provider configured — set GEMINI_API_KEY in .env.")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--naive", action="store_true", help="thin prompt only")
    ap.add_argument("--both", action="store_true", help="naive then shipping, with the delta")
    args = ap.parse_args()

    rows = load()
    print(f"{len(rows)} labelled utterances from {DATASET.relative_to(ROOT)}")
    if args.naive or args.both:
        naive = report(run(rows, "naive", naive=True), "thin baseline prompt")
    if not args.naive:
        shipping = report(run(rows, "shipping", naive=False), "shipping prompt")
    if args.both:
        print(f"\ndelta: {naive}/{len(rows)} -> {shipping}/{len(rows)} "
              f"({shipping - naive:+d}). Report it as a range across runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
