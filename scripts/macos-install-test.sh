#!/usr/bin/env bash
# Clean-install test harness for the macOS (Apple `container`) path.
#
# Exercises install.sh the way a brand-new user would, inside a throwaway
# macOS account you create and delete from the CLI. install.sh's entire
# footprint is user-home-local — the pipx venv under ~/.local (or the pip
# --user tree under ~/Library/Python) and the ~/.bot-bottle config dir. It
# writes no shell-profile PATH line, and never installs the backend (see
# the header of install.sh), so deleting the user is a complete,
# deterministic reset of everything the installer touched. The Apple
# `container` runtime is a HOST prerequisite installed once and kept;
# `deep-reset` is the rare escape hatch that also removes it.
#
# Why a throwaway user and not a disposable VM: bot-bottle's default macOS
# backend is Apple `container`, which runs each container in its own
# Virtualization.framework microVM. Running that backend inside a macOS
# guest VM needs nested virtualization, which Apple gates to M3+ silicon.
# On M1/M2 a separate user account is the only way to get a clean $HOME
# while still reaching the real host backend. Full rationale in
# docs/research/testing-clean-install-on-macos.md.
#
# Usage:
#   sudo ./scripts/macos-install-test.sh test        # up -> run -> status -> down
#   sudo ./scripts/macos-install-test.sh up          # create the throwaway user
#   sudo ./scripts/macos-install-test.sh run         # run install.sh (+doctor) as it
#        ./scripts/macos-install-test.sh status      # user present? backend ready?
#   sudo ./scripts/macos-install-test.sh down        # delete user + home (the reset)
#   sudo ./scripts/macos-install-test.sh deep-reset  # ALSO uninstall host `container`
#
# `test` is the one-shot clean cycle and the command you normally want: it
# refuses to start if the account already exists (a reused home is not a clean
# install), and it tears the account down on the way out however it exits, so
# a failed run never leaves an orphan behind. It exits non-zero if the install
# fails, if `bot-bottle` is missing from the new user's PATH, or if `doctor`
# reports unmet prerequisites — note install.sh itself exits 0 in that last
# case, so `test` is a stricter gate than running the installer by hand.
#
# Config via env:
#   BB_TEST_USER         account short name             (default: bbtest)
#   BB_TEST_FULLNAME     account full name              (default: "bot-bottle install test")
#   BB_TEST_ADMIN        1=admin (reach container svc), 0=standard  (default: 1)
#   BB_TEST_INSTALL_URL  curl this install.sh instead of piping the local checkout
#   BB_TEST_KEEP         1=`test` skips its teardown, to poke at a failure
#   BOT_BOTTLE_INSTALL_SPEC   passed through to install.sh (pip / git spec)
#
# Notes:
#   * Run from a normally-booted admin session. Grant Terminal *Full Disk
#     Access* (System Settings -> Privacy & Security) or `down` half-fails
#     with error -14120 and leaves an orphaned account.
#   * `sysadminctl` always exits 0 even on failure, so `up`/`down` verify
#     the result with `dscl` and fail loudly on a mismatch.
#   * The account is created without a password: `run` drives it headlessly
#     via `sudo -u`, which never needs the target's password. The account
#     cannot GUI-login, which this harness does not require.
#   * `run` covers the installer + `bot-bottle doctor`. Actually launching a
#     bottle from the throwaway user may need a full launchd user session
#     (`launchctl asuser`); on M1/M2 the backend can't run under nested virt
#     anyway, so this harness stops at install + doctor.

set -euo pipefail

USER_NAME="${BB_TEST_USER:-bbtest}"
FULL_NAME="${BB_TEST_FULLNAME:-bot-bottle install test}"
ADMIN="${BB_TEST_ADMIN:-1}"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"

# Set by `test`, which chains the steps itself and so suppresses the
# "here's the next command to run" hints the individual steps print.
IN_TEST=0

# --- guards ----------------------------------------------------------
require_macos() {
    [ "$(uname -s)" = "Darwin" ] \
        || { echo "error: this harness is macOS-only (uname is $(uname -s))" >&2; exit 1; }
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "error: '$1' needs root; re-run under sudo" >&2
        exit 1
    fi
}

user_exists() { dscl . -read "/Users/$USER_NAME" >/dev/null 2>&1; }

# Run a shell snippet as the throwaway user in a fresh login shell.
run_as_user() { sudo -u "$USER_NAME" -i sh -c "$1"; }

# `bot-bottle doctor` as the throwaway user. Non-zero when the entry point
# never made it onto that user's PATH, or when doctor itself is unhappy.
doctor_as_user() {
    if ! run_as_user 'command -v bot-bottle >/dev/null 2>&1'; then
        echo "  bot-bottle is not on PATH for $USER_NAME"
        return 1
    fi
    run_as_user 'bot-bottle doctor'
}

# --- commands --------------------------------------------------------
cmd_up() {
    require_macos
    require_root up
    if user_exists; then
        echo "$USER_NAME already exists; nothing to do (run 'down' first to reset)"
        return 0
    fi
    local admin_flag=()
    [ "$ADMIN" = "1" ] && admin_flag=(-admin)
    # No -password: the account is only ever driven headlessly via `sudo -u`,
    # which doesn't need one. sysadminctl warns about FileVault here; that's
    # irrelevant to a headless test account.
    sysadminctl -addUser "$USER_NAME" -fullName "$FULL_NAME" "${admin_flag[@]}" || true
    # sysadminctl exits 0 regardless of outcome, so confirm the account landed.
    user_exists || { echo "error: failed to create $USER_NAME" >&2; return 1; }
    if [ "$IN_TEST" = 1 ]; then
        echo "created $USER_NAME (admin=$ADMIN)"
    else
        echo "created $USER_NAME (admin=$ADMIN). Install into it with: sudo $0 run"
    fi
}

cmd_run() {
    require_macos
    require_root run
    user_exists || { echo "error: $USER_NAME does not exist; run 'sudo $0 up' first" >&2; return 1; }

    local spec_env=""
    [ -n "${BOT_BOTTLE_INSTALL_SPEC:-}" ] \
        && spec_env="BOT_BOTTLE_INSTALL_SPEC='$BOT_BOTTLE_INSTALL_SPEC' "

    echo "== installing bot-bottle as $USER_NAME =="
    if [ -n "${BB_TEST_INSTALL_URL:-}" ]; then
        run_as_user "curl -fsSL '$BB_TEST_INSTALL_URL' | ${spec_env}sh"
    else
        # Test THIS checkout's install.sh, not the published one, so a PR is
        # verifiable before it lands. Feed it in on stdin rather than staging a
        # copy somewhere the throwaway user can read: the redirect is opened by
        # root before sudo drops privileges, so the tester's mode-700 home is a
        # non-issue, there's no temp file to leak if the run is interrupted, and
        # `sh -s` is the same shape as the documented `curl … | sh` install.
        run_as_user "${spec_env}sh -s" < "$_REPO_ROOT/install.sh"
    fi
    [ "$IN_TEST" = 1 ] \
        || echo "== install.sh runs 'doctor' itself; re-check anytime with: $0 status =="
}

# Informational, with one teeth-bearing case: when it can actually reach
# doctor (root, account present) its exit status is doctor's, so `test` and
# any other caller can use it as the post-install assertion.
cmd_status() {
    require_macos
    local rc=0
    if user_exists; then
        echo "user:    $USER_NAME present"
        if [ "$(id -u)" -eq 0 ]; then
            echo "doctor (as $USER_NAME):"
            doctor_as_user || rc=1
        else
            echo "  (re-run under sudo to run 'bot-bottle doctor' as $USER_NAME)"
        fi
    else
        echo "user:    $USER_NAME absent"
    fi
    if command -v container >/dev/null 2>&1; then
        echo "backend: apple 'container' present ($(container --version 2>/dev/null | head -1))"
    else
        echo "backend: apple 'container' NOT on PATH (host prerequisite; install once)"
    fi
    return "$rc"
}

cmd_down() {
    require_macos
    require_root down
    if ! user_exists; then
        echo "$USER_NAME not present; nothing to remove"
        return 0
    fi
    # A plain -deleteUser removes the home dir, which is the whole reset.
    # -secure is a no-op on modern macOS (secure erase of the home folder
    # was removed in Sierra), so it buys nothing here.
    sysadminctl -deleteUser "$USER_NAME" || true
    if user_exists; then
        echo "error: $USER_NAME still present after delete." >&2
        echo "  - grant Terminal Full Disk Access (System Settings > Privacy & Security), or" >&2
        echo "  - it may hold the last Secure Token (won't happen while another admin exists)" >&2
        return 1
    fi
    echo "removed $USER_NAME and its home — install surface is clean."
}

# Teardown half of `test`, installed as an EXIT trap the moment the account
# exists so that a failure — or a Ctrl-C — still leaves the machine clean.
_test_teardown() {
    local rc=$?
    trap - EXIT INT TERM
    if [ "${BB_TEST_KEEP:-0}" = "1" ]; then
        echo
        echo "== [4/4] down: SKIPPED (BB_TEST_KEEP=1) =="
        echo "   $USER_NAME is still around; remove it with: sudo $0 down"
        exit "$rc"
    fi
    echo
    echo "== [4/4] down =="
    cmd_down || rc=1
    if [ "$rc" -eq 0 ]; then
        echo
        echo "PASS: a brand-new user can install bot-bottle and pass doctor."
    else
        echo
        echo "FAIL: see above (the throwaway account was torn down regardless)." >&2
    fi
    exit "$rc"
}

cmd_test() {
    require_macos
    require_root test
    # A pre-existing account means a pre-existing home, which is the one thing
    # this harness exists to rule out. Don't silently test a dirty install.
    if user_exists; then
        echo "error: $USER_NAME already exists, so this would not be a clean install." >&2
        echo "  reset first:  sudo $0 down" >&2
        return 1
    fi
    IN_TEST=1

    echo "== [1/4] up =="
    cmd_up
    trap _test_teardown EXIT INT TERM

    echo
    echo "== [2/4] run =="
    cmd_run

    echo
    echo "== [3/4] status =="
    # install.sh exits 0 even when doctor reports unmet prerequisites, so the
    # install succeeding is not the verdict — this is.
    cmd_status || {
        echo "error: doctor is unhappy for a freshly installed user (see above)." >&2
        echo "  re-run with BB_TEST_KEEP=1 to keep $USER_NAME around and dig in." >&2
        return 1
    }
}

cmd_deep_reset() {
    require_macos
    require_root deep-reset
    # Remove the user first (idempotent), then the HOST-level container
    # runtime that a user deletion leaves behind under /usr/local + launchd.
    cmd_down || true
    if command -v container >/dev/null 2>&1; then
        # The service can run in more than one launchd context (the invoking
        # user's and root's), so stop both, best-effort.
        [ -n "${SUDO_USER:-}" ] && sudo -u "$SUDO_USER" container system stop 2>/dev/null || true
        container system stop 2>/dev/null || true
        if [ -x /usr/local/bin/uninstall-container.sh ]; then
            /usr/local/bin/uninstall-container.sh -d || true
            echo "uninstalled the host Apple 'container' runtime"
        else
            echo "note: /usr/local/bin/uninstall-container.sh not found; runtime left as-is" >&2
        fi
    else
        echo "no 'container' runtime on PATH; nothing further to remove"
    fi
}

case "${1:-}" in
    test) cmd_test ;;
    up) cmd_up ;;
    run) cmd_run ;;
    status) cmd_status ;;
    down) cmd_down ;;
    deep-reset) cmd_deep_reset ;;
    *) echo "usage: $0 {test|up|run|status|down|deep-reset}" >&2 ; exit 2 ;;
esac
