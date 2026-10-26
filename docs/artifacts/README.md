# Captured evidence

Harness output saved here so the project can be demonstrated without a
network or a model key. These are **not committed yet**: they need a model
provider configured (see `.env.example`). Capture them with:

```bash
python scripts/redteam_injection.py  | tee docs/artifacts/injection_defense_transcript.txt
python scripts/compare_chunking.py   | tee docs/artifacts/chunking_ab_test.txt
python scripts/eval_intents.py --both | tee docs/artifacts/eval_matrix.txt
python scripts/run_acceptance.py     | tee docs/artifacts/acceptance_matrix.txt
```

Each transcript records its run date and which provider answered. Re-run the
intent eval more than once and report the spread: temperature 0 is not
determinism.
