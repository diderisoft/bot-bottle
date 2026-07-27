#!/usr/bin/env bash
# Clean-install test harness for the macOS (Apple `container`) path.
#
# Exercises install.sh the way a brand-new user would, inside a throwaway
# macOS account you create and delete from the CLI. install.sh's entire
# footprint is user-home-local — the pipx venv under ~/.local, or the private
# venv at ~/.bot-bottle/venv plus a ~/.local/bin symlink, and the ~/.bot-bottle
# config dir. It writes no shell-profile PATH line, and never installs the
# backend (see
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
#   sudo ./scripts/macos-install-test.sh test-ready  # ... with prereqs satisfied
#   sudo ./scripts/macos-install-test.sh up          # create the throwaway user
#   sudo ./scripts/macos-install-test.sh run         # run install.sh (+doctor) as it
#   sudo ./scripts/macos-install-test.sh prereqs     # start the per-user backend svc
#        ./scripts/macos-install-test.sh status      # user present? backend ready?
#   sudo ./scripts/macos-install-test.sh down        # delete user + home (the reset)
#   sudo ./scripts/macos-install-test.sh deep-reset  # ALSO uninstall host `container`
#
# TWO TEST VARIANTS, because "does the installer work" and "can a new user
# actually run a bottle" are different questions with different answers:
#
#   test        A macOS system WITHOUT the prerequisites set up for this user —
#               the default state of any brand-new account, since the Apple
#               `container` service is per-user. Asserts the INSTALL is sound
#               (entry point present, doctor runs without crashing, python and
#               config check out). Backend readiness is reported, not required:
#               install.sh does not provide a backend and cannot regress one,
#               so failing on it would make this permanently red.
#
#   test-ready  A macOS system WITH the prerequisites satisfied — it starts the
#               per-user `container` service (and installs its guest kernel)
#               for the throwaway user first, then demands doctor go fully
#               green, backend included. This is the end-to-end claim: a new
#               user can actually run a bottle. Note it downloads a guest
#               kernel per run, because the previous run's went with the
#               deleted home; BB_TEST_KERNEL_INSTALL=0 skips that.
#
# Both refuse to start if the account already exists (a reused home is not a
# clean install), and both tear the account down on the way out however they
# exit, so a failed run never leaves an orphan behind. install.sh itself exits
# 0 when doctor reports unmet prerequisites, so either variant is a stricter
# gate than running the installer by hand.
#
# Config via env:
#   BB_TEST_USER         account short name             (default: bbtest)
#   BB_TEST_FULLNAME     account full name              (default: "bot-bottle install test")
#   BB_TEST_ADMIN        1=admin (reach container svc), 0=standard  (default: 1)
#   BB_TEST_INSTALL_URL  curl this install.sh instead of piping the local checkout
#   BB_TEST_KEEP         1=`test` skips its teardown, to poke at a failure
#   BB_TEST_REQUIRE_BACKEND  1=`test` also fails when no backend is ready
#   BB_TEST_KERNEL_INSTALL   0=`test-ready` skips the guest-kernel download
#   BB_TEST_REPO_URL     https repo the throwaway user clones (default: this one)
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
# https, not the ssh `origin`: the throwaway user has no key of ours.
BB_TEST_REPO_URL="${BB_TEST_REPO_URL:-https://gitea.dideric.is/didericis/bot-bottle.git}"

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

user_uid() {
    dscl . -read "/Users/$USER_NAME" UniqueID 2>/dev/null | awk '{print $2}'
}

# Whether we can enter the target user's launchd domain.
#
# This matters more than it sounds. `container system start` registers
# com.apple.container.apiserver as a per-USER launchd agent and then talks to
# it over XPC. Under plain `sudo -u` the caller is still in root's bootstrap
# namespace, so the lookup crosses domains and the apiserver answers
#     invalidState: "unauthorized request"
# even though it launched fine. `launchctl asuser <uid>` puts the whole
# invocation in that user's domain, which is also what a real user gets from
# Terminal — so it is the more faithful way to run *everything* here, not just
# the service start. Falls back to plain sudo when unavailable, since a
# never-logged-in account may have no bootstrappable domain at all.
_SESSION_OK=""
user_session_available() {
    if [ -z "$_SESSION_OK" ]; then
        local uid
        uid="$(user_uid)"
        if [ -n "$uid" ] && launchctl asuser "$uid" true >/dev/null 2>&1; then
            _SESSION_OK=1
        else
            _SESSION_OK=0
        fi
    fi
    [ "$_SESSION_OK" = "1" ]
}

# Run a shell snippet as the throwaway user in a fresh login shell.
#
# The snippet goes in on stdin, never as an argument. `sudo -i` joins its argv
# into a single string and hands that to the login shell's -c, so quoting in an
# argument is not preserved and a newline ends the command outright ("sh: -c:
# line 1: syntax error: unexpected end of file"). Feeding `sh -s` from stdin
# sidesteps the joining entirely and lets snippets be multi-line.
run_as_user() {
    if user_session_available; then
        printf '%s\n' "$1" | launchctl asuser "$(user_uid)" sudo -u "$USER_NAME" -i sh -s
    else
        printf '%s\n' "$1" | sudo -u "$USER_NAME" -i sh -s
    fi
}

# `bot-bottle doctor` as the throwaway user. Non-zero when no entry point was
# installed at all, or when doctor itself is unhappy.
#
# Deliberately does NOT require `bot-bottle` on PATH: install.sh prints the
# PATH line rather than editing a shell profile, so on a fresh account the
# entry point is installed and working but not on PATH. Demanding PATH here
# would fail every run for a reason the installer intends.
#
# Doctor's own exit code conflates two unrelated things: whether the install
# works, and whether this user has a backend ready to run a bottle. Those need
# different verdicts here. install.sh explicitly does not install a backend,
# and the Apple `container` service is per-USER — its state lives in
# ~/Library/Application Support/com.apple.container — so a brand-new account
# never has one running, no matter how correct the install is. Failing on that
# would leave `test` permanently red for something the installer cannot fix
# and cannot regress. So: assert the install, report the backend.
# BB_TEST_REQUIRE_BACKEND=1 makes backend readiness fatal too.
doctor_as_user() {
    local out rc=0 bad=0
    out="$(mktemp "${TMPDIR:-/tmp}/bb-doctor.XXXXXX")"
    # shellcheck disable=SC2016  # $HOME/$bb must expand in the *target* user's
    # shell, not in this one — that's the whole point of the single quotes.
    run_as_user '
        for bb in bot-bottle "$HOME/.local/bin/bot-bottle" "$HOME/.bot-bottle/venv/bin/bot-bottle"; do
            if command -v "$bb" >/dev/null 2>&1; then
                case "$bb" in
                    bot-bottle) : ;;
                    *) echo "  (not on PATH — running $bb directly, as install.sh advises)" ;;
                esac
                exec "$bb" doctor
            fi
        done
        echo "  no bot-bottle entry point found for this user" >&2
        exit 1
    ' >"$out" 2>&1 || rc=$?
    cat "$out"

    # An unhandled exception is always an install/product defect, never an
    # environment fact — this is exactly how the PermissionError on `ip` showed
    # up, and doctor's non-zero exit alone would not have distinguished it.
    if grep -q 'Traceback (most recent call last)' "$out"; then
        echo "  doctor crashed (traceback above) — a defect, not a missing prerequisite" >&2
        bad=1
    fi
    grep -qE '^ok: +python:' "$out" \
        || { echo "  doctor never reported a usable python" >&2; bad=1; }
    grep -qE '^ok: +config:' "$out" \
        || { echo "  doctor never reported a usable config dir" >&2; bad=1; }
    if [ "$rc" -ne 0 ] && ! grep -qE '^(fail|warn): +backend' "$out"; then
        echo "  doctor failed for something other than backend readiness" >&2
        bad=1
    fi

    local backend_ready=1
    grep -qE '^fail: +backend' "$out" && backend_ready=0
    rm -f "$out"

    [ "$bad" -eq 0 ] || return 1
    if [ "$backend_ready" -eq 0 ]; then
        if [ "${BB_TEST_REQUIRE_BACKEND:-0}" = "1" ]; then
            echo "  no backend is ready and BB_TEST_REQUIRE_BACKEND=1" >&2
            return 1
        fi
        echo "  note: install is sound; no backend ready for this user."
        echo "        Expected on a fresh account — the Apple 'container' service is"
        echo "        per-user and needs one 'container system start'. Set"
        echo "        BB_TEST_REQUIRE_BACKEND=1 to treat this as a failure."
    fi
    return 0
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

# The package spec to install. install.sh's own default is the repo's DEFAULT
# BRANCH, which would mean piping this checkout's installer into the throwaway
# user and then installing main's code — verifying the PR's install.sh against
# a package that doesn't contain the PR. Pin to the branch under test instead.
#
# The branch has to be pushed: the throwaway user clones over https and cannot
# read this checkout (mode-700 home, and no SSH key for the ssh remote), so
# what it gets is whatever the remote has. Say so when that differs from here.
resolve_install_spec() {
    if [ -n "${BOT_BOTTLE_INSTALL_SPEC:-}" ]; then
        echo "$BOT_BOTTLE_INSTALL_SPEC"
        return 0
    fi
    local branch
    branch="$(git -C "$_REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)" || branch=""
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        echo "note: not on a named branch; installing the repo default" >&2
        echo "git+${BB_TEST_REPO_URL}"
        return 0
    fi
    if [ -n "$(git -C "$_REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
        echo "warning: working tree is dirty — the throwaway user installs" >&2
        echo "         $branch as pushed, not what is on disk here." >&2
    elif ! git -C "$_REPO_ROOT" diff --quiet "@{upstream}" 2>/dev/null; then
        echo "warning: $branch differs from its upstream — push first, or the" >&2
        echo "         throwaway user installs stale code." >&2
    fi
    echo "git+${BB_TEST_REPO_URL}@${branch}"
}

cmd_run() {
    require_macos
    require_root run
    user_exists || { echo "error: $USER_NAME does not exist; run 'sudo $0 up' first" >&2; return 1; }

    local spec
    spec="$(resolve_install_spec)"

    echo "== installing bot-bottle as $USER_NAME =="
    echo "== spec: $spec =="
    if [ -n "${BB_TEST_INSTALL_URL:-}" ]; then
        run_as_user "BOT_BOTTLE_INSTALL_SPEC='$spec' curl -fsSL '$BB_TEST_INSTALL_URL' | sh"
    else
        # Test THIS checkout's install.sh, not the published one, so a PR is
        # verifiable before it lands. Feed it in on stdin rather than staging a
        # copy somewhere the throwaway user can read: the redirect is opened by
        # root before sudo drops privileges, so the tester's mode-700 home is a
        # non-issue, there's no temp file to leak if the run is interrupted, and
        # `sh -s` is the same shape as the documented `curl … | sh` install.
        #
        # The spec is prepended to the stream as an export rather than passed
        # as an argument, for the same argv-joining reason as run_as_user.
        {
            printf "export BOT_BOTTLE_INSTALL_SPEC='%s'\n" "$spec"
            cat "$_REPO_ROOT/install.sh"
        } | sudo -u "$USER_NAME" -i sh -s
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

# Satisfy the throwaway user's backend prerequisites.
#
# `bot-bottle backend setup --backend=macos-container` deliberately does NOT do
# this: it only checks, then tells you to run `container system start` yourself
# (see backend/macos_container/setup.py — "no privileged host setup required").
# So the harness runs the command the product asks for, as the user who needs
# it, which is the whole prerequisite for the macOS backend given the CLI is
# already installed host-wide.
cmd_prereqs() {
    require_macos
    require_root prereqs
    user_exists || { echo "error: $USER_NAME does not exist" >&2; return 1; }
    command -v container >/dev/null 2>&1 || {
        echo "error: the Apple 'container' CLI is not on PATH. It is a HOST" >&2
        echo "  prerequisite this harness does not install; see the header." >&2
        return 1
    }
    echo "starting the per-user Apple 'container' service for $USER_NAME"
    user_session_available || {
        echo "warning: no launchd session for $USER_NAME; the service start is" >&2
        echo "  likely to fail with 'unauthorized request'. See user_session_available." >&2
    }
    # The kernel flag is not optional for a headless run. `container system
    # start` PROMPTS for the default Linux kernel when neither flag is given
    # ("default: prompt user"), and a throwaway account has no kernel because
    # it lives in the per-user app root — so the prompt always fires here and
    # dies on "failed to read user input" with no TTY.
    #
    # Enabling it is also the honest choice for what this variant claims: the
    # backend cannot actually run a bottle without a guest kernel. The cost is
    # that every test-ready run downloads one afresh, since the previous run's
    # copy went with the deleted home. BB_TEST_KERNEL_INSTALL=0 skips the
    # download when you only care that the service comes up.
    local kernel_flag=--enable-kernel-install
    [ "${BB_TEST_KERNEL_INSTALL:-1}" = "0" ] && kernel_flag=--disable-kernel-install
    echo "  ($kernel_flag)"
    run_as_user "container system start $kernel_flag" || {
        echo "error: 'container system start' failed for $USER_NAME" >&2
        echo "  invalidState/\"unauthorized request\" means the CLI could not reach" >&2
        echo "    its per-user apiserver over XPC — a never-GUI-logged-in account may" >&2
        echo "    have no usable launchd domain; log into $USER_NAME once to get one." >&2
        echo "  A download failure means the guest kernel could not be fetched; retry" >&2
        echo "    with network, or BB_TEST_KERNEL_INSTALL=0 to skip it." >&2
        return 1
    }
    run_as_user 'container system status' || {
        echo "error: the service is still not running for $USER_NAME" >&2
        return 1
    }
}

# Teardown half of the test cycles, installed as an EXIT trap the moment the
# account exists so a failure — or a Ctrl-C — still leaves the machine clean.
_test_teardown() {
    local rc=$?
    trap - EXIT INT TERM
    if [ "${BB_TEST_KEEP:-0}" = "1" ]; then
        echo
        echo "== [$_STEPS/$_STEPS] down: SKIPPED (BB_TEST_KEEP=1) =="
        echo "   $USER_NAME is still around; remove it with: sudo $0 down"
        exit "$rc"
    fi
    echo
    echo "== [$_STEPS/$_STEPS] down =="
    cmd_down || rc=1
    if [ "$rc" -eq 0 ]; then
        echo
        echo "PASS: $_PASS_CLAIM"
    else
        echo
        echo "FAIL: see above (the throwaway account was torn down regardless)." >&2
    fi
    exit "$rc"
}

# The two variants differ only in whether the backend prerequisites get
# satisfied before doctor runs, which is exactly the question each one asks:
#
#   test        a brand-new user on a machine where nothing has been set up for
#               them. Asserts the INSTALL is sound; backend readiness is
#               reported, not required, because install.sh does not provide a
#               backend and cannot regress one.
#   test-ready  the same user with the documented prerequisites satisfied.
#               Asserts doctor goes fully green, backend included — the
#               end-to-end "a new user can actually run a bottle" claim.
_test_cycle() {
    local with_prereqs="$1"
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

    echo "== [1/$_STEPS] up =="
    cmd_up
    trap _test_teardown EXIT INT TERM

    echo
    echo "== [2/$_STEPS] run =="
    cmd_run

    if [ "$with_prereqs" = 1 ]; then
        echo
        echo "== [3/$_STEPS] prereqs =="
        cmd_prereqs || {
            echo "error: could not satisfy the backend prerequisites (see above)." >&2
            return 1
        }
    fi

    echo
    echo "== [$((_STEPS - 1))/$_STEPS] status =="
    # install.sh exits 0 even when doctor reports unmet prerequisites, so the
    # install succeeding is not the verdict — this is.
    cmd_status || {
        echo "error: doctor is unhappy for a freshly installed user (see above)." >&2
        echo "  re-run with BB_TEST_KEEP=1 to keep $USER_NAME around and dig in." >&2
        return 1
    }
}

cmd_test() {
    _STEPS=4
    _PASS_CLAIM="a brand-new user can install bot-bottle; the install is sound."
    _test_cycle 0
}

cmd_test_ready() {
    _STEPS=5
    _PASS_CLAIM="a brand-new user with the prerequisites met passes doctor outright."
    # With the prerequisites satisfied there is no excuse for an unready
    # backend, so this variant demands the thing `test` only reports.
    BB_TEST_REQUIRE_BACKEND=1
    _test_cycle 1
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
    test-ready) cmd_test_ready ;;
    up) cmd_up ;;
    run) cmd_run ;;
    prereqs) cmd_prereqs ;;
    status) cmd_status ;;
    down) cmd_down ;;
    deep-reset) cmd_deep_reset ;;
    *) echo "usage: $0 {test|test-ready|up|run|prereqs|status|down|deep-reset}" >&2 ; exit 2 ;;
esac
