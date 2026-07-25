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
