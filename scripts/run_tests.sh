#!/usr/bin/env bash
# Every offline suite. No model API, no network (beyond an optional local
# Postgres, which test_backends skips if it is down). Free and fast enough
# to run on every change — a test that needs the network is a test that
# gets skipped, so none of these do.
#
#   bash scripts/run_tests.sh            # summary
#   VERBOSE=1 bash scripts/run_tests.sh  # every check
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3

failed=()
for suite in test_llm test_dates test_attendance test_staff_lookup test_amend test_auth test_backends test_rag test_documents test_calendar test_certificates test_tracker; do
  # stderr carries the pinned-clock warning on every suite; keep it out of the summary
  out="$("$PY" "scripts/$suite.py" 2>/dev/null)"
  status=$?
  if [ -n "${VERBOSE:-}" ]; then echo "$out"; else
    echo "$out" | grep -E '^FAIL|^       ' || true
    printf '%-20s %s\n' "$suite" "$(echo "$out" | tail -n 1)"
  fi
  [ $status -eq 0 ] || failed+=("$suite")
done

echo
if [ ${#failed[@]} -eq 0 ]; then
  echo "all offline suites passed"
else
  echo "FAILED: ${failed[*]}"
  exit 1
fi
