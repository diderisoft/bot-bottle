#!/usr/bin/env bash
# Test runner. Iterates over test_*.sh files in unit/ and integration/
# (or just one of them when given a `unit` / `integration` argument)
# and runs each as a separate process. Aggregates exit codes and
# prints a summary.
#
# Usage:
#   tests/run_tests.sh                # unit + integration
#   tests/run_tests.sh unit           # unit only
#   tests/run_tests.sh integration    # integration only
#   tests/run_tests.sh path/to/test_x.sh   # one specific file

set -uo pipefail

_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ -t 1 ]; then
  C_PASS=$'\033[32m'
  C_FAIL=$'\033[31m'
  C_HEAD=$'\033[36m'
  C_RESET=$'\033[0m'
else
  C_PASS=""
  C_FAIL=""
  C_HEAD=""
  C_RESET=""
fi

usage() {
  cat <<EOF
usage: $(basename "$0") [unit|integration|path/to/test.sh]
  no arg            run unit + integration
  unit              run only tests/unit/test_*.sh
  integration       run only tests/integration/test_*.sh
  <path>            run a single test file
EOF
}

# Collect test files.
declare -a FILES=()
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  unit)        FILES=("${_dir}"/unit/test_*.sh) ;;
  integration) FILES=("${_dir}"/integration/test_*.sh) ;;
  "")          FILES=("${_dir}"/unit/test_*.sh "${_dir}"/integration/test_*.sh) ;;
  *)
    if [ -f "$1" ]; then
      FILES=("$1")
    else
      printf 'no such file: %s\n' "$1" >&2
      usage
      exit 2
    fi
    ;;
esac

# Filter out non-existent globs (no matching files).
declare -a EXISTING=()
for f in "${FILES[@]}"; do
  [ -f "$f" ] && EXISTING+=("$f")
done

if [ "${#EXISTING[@]}" -eq 0 ]; then
  printf 'no test files found\n' >&2
  exit 2
fi

PASS_COUNT=0
FAIL_COUNT=0
declare -a FAIL_FILES=()

for f in "${EXISTING[@]}"; do
  rel="${f#${_dir}/}"
  printf '%s== %s ==%s\n' "$C_HEAD" "$rel" "$C_RESET"
  if bash "$f"; then
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAIL_FILES+=("$rel")
  fi
  printf '\n'
done

# Summary.
TOTAL=$((PASS_COUNT + FAIL_COUNT))
printf '%ssummary%s: %d/%d test files passed\n' "$C_HEAD" "$C_RESET" "$PASS_COUNT" "$TOTAL"
if [ "$FAIL_COUNT" -gt 0 ]; then
  printf '%sfailed%s:\n' "$C_FAIL" "$C_RESET"
  for f in "${FAIL_FILES[@]}"; do
    printf '  - %s\n' "$f"
  done
  exit 1
fi
printf '%sall tests passed%s\n' "$C_PASS" "$C_RESET"
