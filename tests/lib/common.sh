#!/usr/bin/env bash
# Common scaffolding for every test file. Sources assert.sh and computes
# REPO_ROOT so tests can `. "${REPO_ROOT}/lib/<x>.sh"` to load the code
# they're exercising.

if [ -n "${CLAUDE_BOTTLE_TESTS_COMMON_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_TESTS_COMMON_SOURCED=1

set -euo pipefail

_tests_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_ROOT="$_tests_dir"
REPO_ROOT="$(CDPATH= cd -- "${TESTS_ROOT}/.." && pwd)"

# shellcheck source=./assert.sh
. "${TESTS_ROOT}/lib/assert.sh"
# shellcheck source=./fixtures.sh
. "${TESTS_ROOT}/lib/fixtures.sh"
