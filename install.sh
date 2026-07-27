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
# installs the package with pipx (falling back to a private venv), creates the
# config dir, and runs `bot-bottle doctor`. It is idempotent (safe to re-run)
# and never uses sudo. It does NOT install Docker or a VM backend for you —
# `doctor` reports what's missing after install.
#
# Env:
#   BOT_BOTTLE_PYTHON        interpreter to install with (skips the search)
#   BOT_BOTTLE_INSTALL_SPEC  pip/git spec to install instead of the default
#   BOT_BOTTLE_CHANNEL       production (default) or staging
#   BOT_BOTTLE_VERSION       exact qualified vX.Y.Z[-rc.N] release
#   BOT_BOTTLE_REF           exact 40-character commit snapshot (warns untested)
#   BOT_BOTTLE_VENV          where the non-pipx install lives (~/.bot-bottle/venv)
set -eu

PACKAGE_SPEC="${BOT_BOTTLE_INSTALL_SPEC:-}"
VENV="${BOT_BOTTLE_VENV:-${HOME}/.bot-bottle/venv}"
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

# --- resolve an immutable published wheel -----------------------------------

if [ -z "${PACKAGE_SPEC}" ]; then
    selectors=0
    [ -n "${BOT_BOTTLE_REF:-}" ] && selectors=$((selectors + 1))
    [ -n "${BOT_BOTTLE_VERSION:-}" ] && selectors=$((selectors + 1))
    [ -n "${BOT_BOTTLE_CHANNEL:-}" ] && selectors=$((selectors + 1))
    [ "${selectors}" -le 1 ] || die \
        "BOT_BOTTLE_REF, BOT_BOTTLE_VERSION, and BOT_BOTTLE_CHANNEL are mutually exclusive."

    downloads="${HOME}/.bot-bottle/downloads"
    mkdir -p "${downloads}"
    PACKAGE_SPEC="$("${PYTHON}" - \
        "${BOT_BOTTLE_REF:-}" \
        "${BOT_BOTTLE_VERSION:-}" \
        "${BOT_BOTTLE_CHANNEL:-production}" \
        "${downloads}" <<'PY'
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ref, version, channel, download_root = sys.argv[1:]
base = "https://gitea.dideric.is/api/packages/didericis/generic"

def fetch_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)

if ref:
    if re.fullmatch(r"[0-9a-f]{40}", ref) is None:
        raise SystemExit("bot-bottle install: error: BOT_BOTTLE_REF must be 40 lowercase hex")
    index_url = f"{base}/bot-bottle-builds/{ref}/bundle-index.json"
    print(
        "bot-bottle install: WARNING: installing an exact commit snapshot; "
        "it may not have passed release qualification.",
        file=sys.stderr,
    )
    selector = f"commit {ref}"
else:
    if version:
        if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?", version) is None:
            raise SystemExit("bot-bottle install: error: invalid BOT_BOTTLE_VERSION")
        pointer_url = f"{base}/bot-bottle-releases/{version}/release.json"
        selector = f"release {version}"
    else:
        if channel not in ("production", "staging"):
            raise SystemExit(
                "bot-bottle install: error: BOT_BOTTLE_CHANNEL must be production or staging")
        pointer_url = f"{base}/bot-bottle-channels/{channel}/channel.json"
        selector = f"{channel} channel"
    pointer = fetch_json(pointer_url)
    if pointer.get("schema") != 1 or pointer.get("qualified") is not True:
        raise SystemExit("bot-bottle install: error: release pointer is not qualified")
    index_url = pointer.get("bundle_index_url", "")
    if not isinstance(index_url, str) or not index_url.startswith("https://"):
        raise SystemExit("bot-bottle install: error: release pointer has no immutable bundle")

index = fetch_json(index_url)
source_commit = index.get("source_commit", "")
if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
    raise SystemExit("bot-bottle install: error: bundle has an invalid source commit")
if ref and source_commit != ref:
    raise SystemExit("bot-bottle install: error: bundle does not match BOT_BOTTLE_REF")
wheel = index.get("wheel")
if not isinstance(wheel, dict):
    raise SystemExit("bot-bottle install: error: bundle has no wheel")
filename, url, expected = (
    wheel.get("filename"), wheel.get("url"), wheel.get("sha256"))
if (
    not isinstance(filename, str)
    or re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", filename) is None
    or not isinstance(url, str)
    or not url.startswith("https://")
    or not url.endswith("/" + filename)
    or not isinstance(expected, str)
    or re.fullmatch(r"[0-9a-f]{64}", expected) is None
):
    raise SystemExit("bot-bottle install: error: bundle wheel metadata is invalid")

destination = Path(download_root) / source_commit / filename
destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_suffix(destination.suffix + ".part")
digest = hashlib.sha256()
with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
    while chunk := response.read(1 << 20):
        digest.update(chunk)
        output.write(chunk)
if digest.hexdigest() != expected:
    temporary.unlink(missing_ok=True)
    raise SystemExit("bot-bottle install: error: downloaded wheel checksum mismatch")
temporary.replace(destination)
print(
    f"bot-bottle install: selected {selector} "
    f"(source {source_commit}, sha256 {expected[:16]}…)",
    file=sys.stderr,
)
print(destination)
PY
    )"
fi

# An explicit `git+` override shells out to git under the hood, whether via
# pipx or pip. Fail early with a clear message rather than deep inside the
# installer's output.
case "${PACKAGE_SPEC}" in
    git+*|*.git)
        command -v git >/dev/null 2>&1 || die \
            "git is required to install from '${PACKAGE_SPEC}'. Install git, or set "\
"BOT_BOTTLE_INSTALL_SPEC to a non-git spec (e.g. a wheel path or a package index name)."
        ;;
esac

# --- config directories ------------------------------------------------------

mkdir -p \
    "${HOME}/.bot-bottle/agents" \
    "${HOME}/.bot-bottle/bottles" \
    "${HOME}/.bot-bottle/contrib"

# --- install -----------------------------------------------------------------

BIN_DIR="${HOME}/.local/bin"

if command -v pipx >/dev/null 2>&1; then
    # --python pins the venv to the interpreter we vetted. Without it pipx uses
    # whichever Python it was itself installed with, which is not necessarily
    # the one that passed the version check above.
    say "installing with pipx (python: ${PYTHON})"
    pipx install --python "${PYTHON}" --force "${PACKAGE_SPEC}"
    # Ask pipx where it puts entry points rather than assuming ~/.local/bin.
    pipx_bin="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
    [ -n "${pipx_bin}" ] && BIN_DIR="${pipx_bin}"
else
    # No `pip install --user` fallback: PEP 668 makes it unusable on nearly
    # every interpreter a Mac offers (Homebrew and python.org are both
    # externally managed), and on Debian/Ubuntu too. A private venv sidesteps
    # that entirely — PEP 668 does not apply inside a venv — and `venv` is
    # stdlib, so unlike pipx there is nothing to bootstrap first.
    say "pipx not found; installing into a managed venv at ${VENV}"
    "${PYTHON}" -m venv --clear "${VENV}" || die \
        "could not create a virtualenv at ${VENV} using ${PYTHON}. On Debian/Ubuntu "\
"the venv module ships separately: 'sudo apt install python3-venv'."
    "${VENV}/bin/python" -m pip install --upgrade "${PACKAGE_SPEC}"
    # Expose the entry point outside the venv, the way pipx would.
    mkdir -p "${BIN_DIR}"
    ln -sf "${VENV}/bin/bot-bottle" "${BIN_DIR}/bot-bottle"
fi

# --- locate the entry point --------------------------------------------------

if command -v bot-bottle >/dev/null 2>&1; then
    BOT_BOTTLE_BIN="bot-bottle"
elif [ -x "${BIN_DIR}/bot-bottle" ]; then
    BOT_BOTTLE_BIN="${BIN_DIR}/bot-bottle"
    # Name the file the user's own login shell actually reads. ~/.profile is
    # the safe default for non-zsh: bash falls back to it, and suggesting
    # ~/.bash_profile could shadow an existing ~/.profile.
    case "${SHELL:-}" in
        */zsh) profile="~/.zprofile" ;;
        *) profile="~/.profile" ;;
    esac
    say "note: add ${BIN_DIR} to your PATH to run 'bot-bottle' directly:"
    say "    echo 'export PATH=\"${BIN_DIR}:\$PATH\"' >> ${profile}"
else
    die "bot-bottle was installed but no entry point turned up in ${BIN_DIR}"
fi

# --- verify ------------------------------------------------------------------

say "running '${BOT_BOTTLE_BIN} doctor'"
if "${BOT_BOTTLE_BIN}" doctor; then
    say "done. Run '${BOT_BOTTLE_BIN} --help' to get started."
else
    say "install completed, but 'doctor' reported unmet prerequisites (see above)."
    say "resolve them, then re-run '${BOT_BOTTLE_BIN} doctor'."
fi
