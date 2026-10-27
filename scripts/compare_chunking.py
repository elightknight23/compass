"""Chunking A/B — naive slices versus structure-aware sections, side by side.

The engineered case lives in docs/regulations/od_and_leave_policy.md. Under
"Long-Duration OD" the rule reads "Such requests must reach the faculty
advisor at least 10 working days before the first day" — a sentence that
never repeats the words "long-duration" or "OD". The heading carries that
meaning. Right above it, under "Standard OD", sits a 2-working-day rule.

The naive 500-character slicer cuts without regard to headings, so the
10-day sentence can land in a chunk that no longer says what it is about,
and the standard-OD chunk wins retrieval. The structured chunker keeps each
section under its breadcrumb, so the vector itself knows which rule it is.

What the naive slicer actually does here (measured on the current corpus):
it cuts the "Long-Duration OD" section in half. One chunk ends with the
heading and "national-level sports camps" but not the rule; the next starts
"…arranged through the Training and Placement Cell" and contains "Such
requests must reach the faculty advisor at least 10 working days before"
with nothing left to say what "such requests" are. The first half matches
the question best, and it carries the 2-day standard rule with it.

k is pinned to 1 for both arms: this compares chunking, so retrieval depth
is held constant. Said plainly: at k=2 the naive arm retrieves both halves
and can reassemble the answer by brute force. A wide window papering over
bad boundaries is a real effect, and you pay for it in tokens and
distractors — better chunks let you retrieve less.

    python scripts/compare_chunking.py | tee docs/artifacts/chunking_ab_test.txt
"""

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm, rag  # noqa: E402

K = 1
QUESTION = (
    "I've been selected for a two-week national-level sports camp and will be "
    "on OD. How far in advance do I have to apply?"
)
CORRECT = "10 working days"
WRONG = "2 working days"


def show(strategy: str, collection: str) -> str:
    print(f"\n{'=' * 76}\n  {strategy.upper()}  (collection: {collection}, k={K})\n{'=' * 76}")
    for rank, h in enumerate(rag.retrieve(QUESTION, collection=collection, k=K), 1):
        meta = h["metadata"]
        where = meta.get("header_path") or f"chunk #{meta.get('chunk_index')}"
        print(f"\n  [{rank}] {meta['source']} :: {where}   (distance {h['distance']:.4f})")
        print(textwrap.indent(textwrap.fill(" ".join(h["text"].split()), width=70), "      "))
    result = rag.answer_from_kb(QUESTION, collection=collection, k=K)
    print(f"\n  ANSWER ({strategy}, via {result['provider']}):")
    print(textwrap.indent(textwrap.fill(result["answer"], width=70), "      "))
    return result["answer"]


def main() -> int:
    if not llm.providers():
        print("No model provider configured — set GEMINI_API_KEY in .env.")
        return 2
    print(f'QUESTION: "{QUESTION}"')
    print(f"GROUND TRUTH: {CORRECT} (od_and_leave_policy.md > Applying for OD > Long-Duration OD)")
    answers = {s: show(s, c) for s, c in (("naive", rag.NAIVE_COLLECTION),
                                          ("structured", rag.STRUCT_COLLECTION))}
    print(f"\n{'=' * 76}\n  VERDICT\n{'=' * 76}")
    for strategy, answer in answers.items():
        if CORRECT in answer and WRONG not in answer:
            verdict = f"correct — {CORRECT}"
        elif WRONG in answer and CORRECT not in answer:
            verdict = f"WRONG — gives the standard-OD figure ({WRONG})"
        elif CORRECT in answer:
            verdict = "mentions both figures — read the answer"
        else:
            verdict = "MISS — could not state the rule"
        print(f"  {strategy:<12} {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
