#!/usr/bin/env bash
# Tiny assertion helpers. No framework — each test file sources this,
# calls `assert_*` functions, and ends with `test_summary` which exits
# 0 if every assertion passed and 1 otherwise.
#
# Counters are file-local: every test process gets its own TEST_PASS /
# TEST_FAIL. run_tests.sh aggregates by exit code, not by reading these.

if [ -n "${CLAUDE_BOTTLE_TESTS_ASSERT_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_TESTS_ASSERT_SOURCED=1

TEST_PASS=0
TEST_FAIL=0
TEST_NAME="${TEST_NAME:-unnamed}"

if [ -t 1 ]; then
  _C_PASS=$'\033[32m'
  _C_FAIL=$'\033[31m'
  _C_SKIP=$'\033[33m'
  _C_RESET=$'\033[0m'
else
  _C_PASS=""
  _C_FAIL=""
  _C_SKIP=""
  _C_RESET=""
fi

_pass() {
  TEST_PASS=$((TEST_PASS + 1))
  printf '  %sPASS%s %s\n' "$_C_PASS" "$_C_RESET" "$1"
}

_fail() {
  TEST_FAIL=$((TEST_FAIL + 1))
  printf '  %sFAIL%s %s\n' "$_C_FAIL" "$_C_RESET" "$1" >&2
  shift
  local line
  for line in "$@"; do
    printf '    %s\n' "$line" >&2
  done
}

assert_eq() {
  local expected="$1" actual="$2" msg="${3:-equal}"
  if [ "$expected" = "$actual" ]; then
    _pass "$msg"
  else
    _fail "$msg" "expected: ${expected}" "actual:   ${actual}"
  fi
}

assert_contains() {
  local haystack="$1" needle="$2" msg="${3:-contains}"
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    _pass "$msg"
  else
    _fail "$msg" "expected to contain: ${needle}" "haystack: ${haystack}"
  fi
}

assert_not_contains() {
  local haystack="$1" needle="$2" msg="${3:-does not contain}"
  if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
    _pass "$msg"
  else
    _fail "$msg" "expected NOT to contain: ${needle}" "haystack: ${haystack}"
  fi
}

assert_match() {
  local haystack="$1" pattern="$2" msg="${3:-matches}"
  if printf '%s' "$haystack" | grep -qE -- "$pattern"; then
    _pass "$msg"
  else
    _fail "$msg" "expected pattern: ${pattern}" "haystack: ${haystack}"
  fi
}

# assert_exit_zero <cmd...> — runs the command, fails the assertion
# if it exits non-zero. Captures stdout+stderr for the failure message.
assert_exit_zero() {
  local label="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then
    _pass "$label"
  else
    _fail "$label" "exit non-zero" "output: ${out}"
  fi
}

assert_exit_nonzero() {
  local label="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then
    _fail "$label" "exit was 0; expected non-zero" "output: ${out}"
  else
    _pass "$label"
  fi
}

skip() {
  printf '  %sSKIP%s %s\n' "$_C_SKIP" "$_C_RESET" "$1"
}

skip_test_if_no_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    printf '%sSKIP%s %s — docker not on PATH\n' "$_C_SKIP" "$_C_RESET" "$TEST_NAME"
    exit 0
  fi
  if ! docker info >/dev/null 2>&1; then
    printf '%sSKIP%s %s — docker daemon unreachable\n' "$_C_SKIP" "$_C_RESET" "$TEST_NAME"
    exit 0
  fi
}

test_summary() {
  printf '\n%s: %d passed, %d failed\n' "$TEST_NAME" "$TEST_PASS" "$TEST_FAIL"
  if [ "$TEST_FAIL" -gt 0 ]; then
    exit 1
  fi
  exit 0
}
