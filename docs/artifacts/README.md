# Captured evidence

Harness output saved here so the project can be demonstrated without a
network or a model key. Each one needs `GEMINI_API_KEY` to re-run (see
`.env.example`):

```bash
python scripts/redteam_injection.py   > docs/artifacts/injection_defense_transcript.txt
python scripts/compare_chunking.py    > docs/artifacts/chunking_ab_test.txt
python scripts/run_acceptance.py      > docs/artifacts/acceptance_matrix.txt
python scripts/eval_intents.py --both > docs/artifacts/eval_matrix.txt
```

| File | Shows |
| :--- | :--- |
| `injection_defense_transcript.txt` | Two planted injections, both retrieved into the model's context: a circular in the built-in regulations, and a "system notice" inside an uploaded syllabus PDF. Both are named as not in force; neither is obeyed. |
| `chunking_ab_test.txt` | Naive 500-character slices against structure-aware chunks, on a question built to straddle a heading. |
| `acceptance_matrix.txt` | Every row through the real graph with the real model, including the uploaded syllabus and the confirmed calendar, plus the dead-database drill. |
| `eval_matrix.txt` | Intent accuracy and confusion matrices, the shipping prompt against a deliberately thin one. |

How to read them:

- **Each transcript records its run date and which model answered.** The
  free tier rate-limits the primary model during a long run, and the rest
  spills onto the fallback. A row answered by the fallback is a different
  measurement, and the summary line counts how many each model answered.
- **Temperature 0 is not determinism.** Re-run the intent eval more than
  once and report the spread, not one number.
- **The eval set and the prompt share an author**, so the score measures
  self-consistency, not resilience to strangers.
- The injection verdicts are heuristics over the answer text. A good answer
  may quote the planted claim in order to reject it, and that counts as a
  pass. Read the answers.
