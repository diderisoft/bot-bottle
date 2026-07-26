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
# This script is a thin bootstrapper: it checks prerequisites, installs the
# package with pipx (falling back to pip --user), creates the config dir, and
# runs `bot-bottle doctor`. It is idempotent (safe to re-run) and never uses
# sudo. It does NOT install Docker or a VM backend for you — `doctor` reports
# what's missing after install.
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

# --- prerequisites -----------------------------------------------------------

command -v python3 >/dev/null 2>&1 \
    || die "python3 ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required but was not found"

python3 - "$MIN_PYTHON_MAJOR" "$MIN_PYTHON_MINOR" <<'PY' || die "python3 ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} or newer is required"
import sys

want = (int(sys.argv[1]), int(sys.argv[2]))
raise SystemExit(0 if sys.version_info[:2] >= want else 1)
PY

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
    python3 -m pip --version >/dev/null 2>&1 || die \
        "neither pipx nor a usable 'python3 -m pip' was found. Install pipx "\
"(recommended): 'python3 -m pip install --user pipx' or your OS package manager."
    if python3 - <<'PY'
import os
import sys
import sysconfig

# PEP 668: an EXTERNALLY-MANAGED marker in the stdlib dir means pip refuses
# to install into this interpreter without --break-system-packages.
marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
raise SystemExit(0 if os.path.exists(marker) else 1)
PY
    then
        die "this Python is externally managed (PEP 668), so 'pip install --user' is "\
"blocked. Install pipx and re-run: 'python3 -m pip install --user --break-system-packages pipx', "\
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
    say "installing with pipx"
    pipx install --force "${PACKAGE_SPEC}"
else
    say "pipx not found; installing with 'python3 -m pip install --user'"
    python3 -m pip install --user --upgrade "${PACKAGE_SPEC}"
fi

# --- locate the entry point --------------------------------------------------

if command -v bot-bottle >/dev/null 2>&1; then
    BOT_BOTTLE_BIN="bot-bottle"
elif [ -x "${HOME}/.local/bin/bot-bottle" ]; then
    BOT_BOTTLE_BIN="${HOME}/.local/bin/bot-bottle"
    say "note: add ${HOME}/.local/bin to your PATH to run 'bot-bottle' directly"
else
    die "bot-bottle was installed but is not on PATH; add ~/.local/bin to PATH and re-run"
fi

# --- verify ------------------------------------------------------------------

say "running '${BOT_BOTTLE_BIN} doctor'"
if "${BOT_BOTTLE_BIN}" doctor; then
    say "done. Run '${BOT_BOTTLE_BIN} --help' to get started."
else
    say "install completed, but 'doctor' reported unmet prerequisites (see above)."
    say "resolve them, then re-run '${BOT_BOTTLE_BIN} doctor'."
fi
