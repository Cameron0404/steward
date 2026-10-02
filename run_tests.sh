#!/bin/bash
# Run every selftest against a temporary STEWARD_HOME. Nothing launches a real session.
# STEWARD_LIVE=1 adds one real run of examples/hello-brief.md on Haiku (needs a logged-in
# `claude` CLI and spends a few cents); without it that step is skipped.
set -u
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export STEWARD_HOME="$TMP" STEWARD_WORKDIR="$TMP"
unset STEWARD_MAX_CONCURRENT STEWARD_LEAN_FLAGS
fail=0
for t in agentctl_selftest.py stewardq_selftest.py statusline/usage_statusline_selftest.py; do
  out="$("$PY" "$t" 2>&1)"; rc=$?
  echo "$out" | tail -1
  if [ $rc -ne 0 ]; then echo "$out" | grep -A2 '^FAIL'; fail=1; fi
done
if [ "${STEWARD_LIVE:-}" = "1" ]; then
  if command -v claude >/dev/null 2>&1; then
    "$PY" agentctl.py run --id live-smoke --brief examples/hello-brief.md --model haiku --max-minutes 5 --max-budget-usd 0.5
    rc=$?; echo "live smoke run exit $rc (0 done)"; [ $rc -eq 0 ] || fail=1
  else
    echo "live smoke run SKIPPED: no claude CLI on PATH"
  fi
else
  echo "live smoke run SKIPPED (set STEWARD_LIVE=1 to run one real Haiku session)"
fi
[ $fail -eq 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit $fail
