#!/bin/sh
# bot-bottle quick installer.
#
# Usage:
#   curl -fsSL https://gitea.dideric.is/didericis/bot-bottle/raw/branch/main/install.sh | sh
#
# Python-native users can skip this entirely:
#   pipx install bot-bottle        # from a checkout or a published index
#   uv tool install bot-bottle
#
# This script is a thin bootstrapper: it finds a Python 3.11+ interpreter,
# installs the package with pipx (falling back to pip --user), creates the
# config dir, and runs `bot-bottle doctor`. It is idempotent (safe to re-run)
# and never uses sudo. It does NOT install Docker or a VM backend for you —
# `doctor` reports what's missing after install.
#
# Env:
#   BOT_BOTTLE_PYTHON        interpreter to install with (skips the search)
#   BOT_BOTTLE_INSTALL_SPEC  pip/git spec to install instead of the default
set -eu

PACKAGE_SPEC="${BOT_BOTTLE_INSTALL_SPEC:-git+https://gitea.dideric.is/didericis/bot-bottle.git}"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11

say() {
    printf 'bot-bottle install: %s\n' "$*" >&2
}

die() {
    say "error: $*"
    exit 1
}

# --- prerequisites: find an interpreter new enough ----------------------------

# Is $1 an interpreter that exists and meets the floor?
python_ok() {
    [ -n "${1:-}" ] || return 1
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" - "$MIN_PYTHON_MAJOR" "$MIN_PYTHON_MINOR" <<'PY' >/dev/null 2>&1
import sys

want = (int(sys.argv[1]), int(sys.argv[2]))
raise SystemExit(0 if sys.version_info[:2] >= want else 1)
PY
}

python_version() {
    "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
}

# `python3` on PATH is often NOT the newest interpreter installed, and on macOS
# it is usually the oldest: a fresh login shell's PATH is just /etc/paths, so
# python3 resolves to the Command Line Tools stub (3.9.x) while the usable
# 3.11+ build sits in /opt/homebrew/bin or a python.org framework directory,
# reachable only via a line in the *installing* user's shell profile. A new
# account inherits none of that. Look past PATH before giving up, so the common
# case installs instead of dead-ending on a version error.
find_python() {
    for candidate in \
        "${BOT_BOTTLE_PYTHON:-}" \
        python3 \
        python3.14 python3.13 python3.12 python3.11 \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        "${HOME}/.local/bin/python3" \
        /Library/Frameworks/Python.framework/Versions/*/bin/python3
    do
        # An unmatched glob arrives here literally; python_ok rejects it.
        if python_ok "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# An explicit choice that doesn't work is an error, not a reason to quietly
# search elsewhere and install somewhere the caller didn't ask for.
if [ -n "${BOT_BOTTLE_PYTHON:-}" ] && ! python_ok "${BOT_BOTTLE_PYTHON}"; then
    if command -v "${BOT_BOTTLE_PYTHON}" >/dev/null 2>&1; then
        die "BOT_BOTTLE_PYTHON=${BOT_BOTTLE_PYTHON} is $(python_version "${BOT_BOTTLE_PYTHON}"), "\
"below the ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} floor. Unset it to search for a newer one."
    fi
    die "BOT_BOTTLE_PYTHON=${BOT_BOTTLE_PYTHON} is not an executable interpreter."
fi

PYTHON="$(find_python || true)"

if [ -z "${PYTHON}" ]; then
    path_python="$(command -v python3 2>/dev/null || true)"
    if [ -n "${path_python}" ]; then
        found="the python3 on your PATH is ${path_python} ($(python_version "${path_python}")), which is too old"
    else
        found="no python3 was found on your PATH"
    fi
    case "$(uname -s)" in
        Darwin) fix="  brew install python@3.12
  # or install from https://www.python.org/downloads/macos/
  # macOS itself ships only /usr/bin/python3, which is too old" ;;
        *) fix="  sudo apt install python3.12    # Debian/Ubuntu
  sudo dnf install python3.12    # Fedora/RHEL" ;;
    esac
    die "bot-bottle needs python3 ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} or newer, and none was found.
  ${found}.
  Also checked: python3.11-3.14, /opt/homebrew/bin, /usr/local/bin,
  ~/.local/bin, and python.org framework builds.

Install a newer Python, then re-run this installer:
${fix}

Already have one somewhere? Point at it directly:
  BOT_BOTTLE_PYTHON=/path/to/python3 sh install.sh"
fi

# Be explicit when the interpreter isn't the obvious one, so nobody is left
# wondering which Python their install ended up on.
path_python="$(command -v python3 2>/dev/null || true)"
if [ "${PYTHON}" != "${path_python}" ]; then
    say "using ${PYTHON} ($(python_version "${PYTHON}"))"
    if [ -n "${path_python}" ]; then
        say "note: 'python3' on your PATH is ${path_python} ($(python_version "${path_python}")), which is below the ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} floor"
    fi
fi

# Installing a `git+` spec (the default) shells out to git under the hood,
# whether via pipx or pip. Fail early with a clear message rather than deep
# inside the installer's output.
case "${PACKAGE_SPEC}" in
    git+*|*.git)
        command -v git >/dev/null 2>&1 || die \
            "git is required to install from '${PACKAGE_SPEC}'. Install git, or set "\
"BOT_BOTTLE_INSTALL_SPEC to a non-git spec (e.g. a wheel path or a package index name)."
        ;;
esac

# The pip fallback needs a usable pip. Externally-managed interpreters
# (PEP 668, common on Debian/Ubuntu/Homebrew) reject `pip install --user`;
# pipx sidesteps that, so recommend it when pip can't be used.
if ! command -v pipx >/dev/null 2>&1; then
    "${PYTHON}" -m pip --version >/dev/null 2>&1 || die \
        "neither pipx nor a usable '${PYTHON} -m pip' was found. Install pipx "\
"(recommended): '${PYTHON} -m pip install --user pipx' or your OS package manager."
    if "${PYTHON}" - <<'PY'
import os
import sys
import sysconfig

# PEP 668: an EXTERNALLY-MANAGED marker in the stdlib dir means pip refuses
# to install into this interpreter without --break-system-packages.
marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
raise SystemExit(0 if os.path.exists(marker) else 1)
PY
    then
        die "${PYTHON} is externally managed (PEP 668), so 'pip install --user' is "\
"blocked. Install pipx and re-run: '${PYTHON} -m pip install --user --break-system-packages pipx', "\
"then 'pipx ensurepath'."
    fi
fi

# --- config directories ------------------------------------------------------

mkdir -p \
    "${HOME}/.bot-bottle/agents" \
    "${HOME}/.bot-bottle/bottles" \
    "${HOME}/.bot-bottle/contrib"

# --- install -----------------------------------------------------------------

if command -v pipx >/dev/null 2>&1; then
    # --python pins the venv to the interpreter we vetted. Without it pipx uses
    # whichever Python it was itself installed with, which is not necessarily
    # the one that passed the version check above.
    say "installing with pipx (python: ${PYTHON})"
    pipx install --python "${PYTHON}" --force "${PACKAGE_SPEC}"
else
    say "pipx not found; installing with '${PYTHON} -m pip install --user'"
    "${PYTHON}" -m pip install --user --upgrade "${PACKAGE_SPEC}"
fi

# --- locate the entry point --------------------------------------------------

# The pip --user scripts directory is platform-specific: ~/.local/bin on Linux,
# but ~/Library/Python/<X.Y>/bin on a python.org macOS interpreter. Ask the
# interpreter for its own user-scheme scripts dir instead of hardcoding.
USER_SCRIPTS="$("${PYTHON}" - <<'PY'
import sysconfig
print(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
PY
)"

if command -v bot-bottle >/dev/null 2>&1; then
    BOT_BOTTLE_BIN="bot-bottle"
elif [ -n "${USER_SCRIPTS}" ] && [ -x "${USER_SCRIPTS}/bot-bottle" ]; then
    BOT_BOTTLE_BIN="${USER_SCRIPTS}/bot-bottle"
    say "note: add ${USER_SCRIPTS} to your PATH to run 'bot-bottle' directly"
else
    die "bot-bottle was installed but is not on PATH; add ${USER_SCRIPTS:-your user scripts dir} to PATH and re-run"
fi

# --- verify ------------------------------------------------------------------

say "running '${BOT_BOTTLE_BIN} doctor'"
if "${BOT_BOTTLE_BIN}" doctor; then
    say "done. Run '${BOT_BOTTLE_BIN} --help' to get started."
else
    say "install completed, but 'doctor' reported unmet prerequisites (see above)."
    say "resolve them, then re-run '${BOT_BOTTLE_BIN} doctor'."
fi
