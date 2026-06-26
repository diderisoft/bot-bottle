#!/usr/bin/env bash
# Combined unit + integration coverage (see docs/decisions/0004-coverage-policy.md).
#
# Runs the unit suite, then appends the integration suite (which skips
# cleanly when Docker / the backend CLIs are unavailable), and prints one
# combined report. The integration suite is what scores the subprocess /
# backend orchestration modules, so the number here is the policy's
# yardstick — not the unit-only badge.
#
# Usage:
#   scripts/coverage.sh            # combined report
#   scripts/coverage.sh critical   # also report just the critical modules
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

# Critical security/logic core held to the high bar by ADR 0004. The list
# lives in one place (scripts/critical-modules.txt) so this report and the
# README "core coverage" badge can't drift; comma-join it for --include.
CRITICAL=$(grep -vE '^[[:space:]]*(#|$)' scripts/critical-modules.txt | paste -sd, -)

rm -f .coverage

echo "== unit ==" >&2
"$PY" -m coverage run -m unittest discover -t . -s tests/unit

echo "== integration (skips without Docker) ==" >&2
"$PY" -m coverage run --append -m unittest discover -t . -s tests/integration

echo "== combined report ==" >&2
"$PY" -m coverage report -m

if [ "${1:-}" = "critical" ]; then
    echo "== critical modules (ADR 0004 target: 90%) ==" >&2
    "$PY" -m coverage report --include="$CRITICAL"
fi
