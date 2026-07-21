#!/usr/bin/env bash
# Combined unit + integration coverage (see docs/decisions/0004-coverage-policy.md).
#
# Two modes:
#
#   scripts/coverage.sh [critical]
#     Run mode (default, for local dev): executes the unit suite then the
#     integration suite under coverage and prints a combined report.
#
#   scripts/coverage.sh aggregate [critical]
#     Aggregate mode (used by CI): combines pre-existing .coverage.* files
#     produced by individual test jobs and prints a combined report. No tests
#     are re-executed; no KVM or Docker dependency.
#
# Pass "critical" as the last argument in either mode to also report just the
# critical modules (ADR 0004 target: 90%).
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

# Critical security/logic core held to the high bar by ADR 0004. The list
# lives in one place (scripts/critical-modules.txt) so this report and the
# README "core coverage" badge can't drift; comma-join it for --include.
CRITICAL=$(grep -vE '^[[:space:]]*(#|$)' scripts/critical-modules.txt | paste -sd, -)

if [ "${1:-}" = "aggregate" ]; then
    # Aggregate mode: combine .coverage.* artifacts already in the workspace.
    echo "== combining coverage artifacts ==" >&2
    "$PY" -m coverage combine

    echo "== combined report ==" >&2
    "$PY" -m coverage report -m

    if [ "${2:-}" = "critical" ]; then
        echo "== critical modules (ADR 0004 target: 90%) ==" >&2
        "$PY" -m coverage report --include="$CRITICAL"
    fi
    exit 0
fi

# Run mode (default): execute both suites under coverage in this process.
rm -f .coverage

echo "== unit ==" >&2
"$PY" -m coverage run -m unittest discover -t . -s tests/unit

echo "== integration (firecracker; skips docker tests) ==" >&2
BOT_BOTTLE_BACKEND=firecracker SKIP_DOCKER_TESTS=1 \
    BOT_BOTTLE_INFRA_ARTIFACT_DIR="${BOT_BOTTLE_CI_INFRA_ARTIFACT_DIR:-}" \
    "$PY" -m coverage run --append -m unittest discover -t . -s tests/integration

echo "== combined report ==" >&2
"$PY" -m coverage report -m

if [ "${1:-}" = "critical" ]; then
    echo "== critical modules (ADR 0004 target: 90%) ==" >&2
    "$PY" -m coverage report --include="$CRITICAL"
fi
