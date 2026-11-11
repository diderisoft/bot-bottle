#!/bin/sh
# bot-bottle quick installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
#
# It installs the latest release: the newest `vX.Y.Z` tag, whose orchestrator
# and gateway images are published. To install main instead (development;
# main's infra images aren't published, so run `bot-bottle up --build`):
#   curl -fsSL "<the URL above>" | sh -s -- --main
#
# Python-native users can skip this entirely, pinning the latest tag:
#   uv tool install "bot-bottle[remote] @ git+https://github.com/diderisoft/bot-bottle.git@v0.1.6"
#   pipx install "bot-bottle[remote] @ git+https://github.com/diderisoft/bot-bottle.git@v0.1.6"
#
# This script is a thin bootstrapper: it finds the latest release tag and a
# Python 3.11+ interpreter, installs the package with uv or pipx if you have
# either (falling back to a private venv), creates the config dir, then runs
# `bot-bottle bootstrap`: `install` (this host's backend setup, tmux,
# autostart), `setup` (starter agents, then an offer to pair a phone) and
# `doctor`. It is idempotent (safe to re-run). It never runs sudo on its own:
# the backend's one root step (the macOS firewall, firecracker's network
# pool) is asked about first, on a terminal, with the exact command shown,
# and printed for you to run otherwise. It does NOT install Docker or a VM
# runtime for you: `doctor` reports what's missing.
#
# Env:
#   BOT_BOTTLE_PYTHON        interpreter to install with (skips the search)
#   BOT_BOTTLE_INSTALL_SPEC  pip/git spec to install instead of the default
#                            (bot-bottle[remote] at the latest release tag; no
#                            extra on Intel Macs)
#   BOT_BOTTLE_INSTALL_REF   git ref to install instead of the latest release
#                            tag, e.g. `main` (what --main sets) or `v0.1.4`
#   BOT_BOTTLE_VENV          where the venv install lives, without uv or pipx (~/.bot-bottle/venv)
#
# With no Python 3.11+ at all, uv supplies one: yours if you have uv, else a
# pinned uv release (checksum-verified) kept privately under ~/.bot-bottle/uv
# with its Python and the bot-bottle tool, so it never touches a uv of yours.
#
# Uninstall (keeps ~/.bot-bottle; pass --purge after --uninstall to delete it):
#   curl -fsSL "<the URL above>" | sh -s -- --uninstall
# This runs `bot-bottle uninstall` (see `bot-bottle uninstall --help`), or, if
# that command is gone or broken, removes the installer's venv and shim and
# says what's left.
set -eu

REPO_SPEC="git+https://github.com/diderisoft/bot-bottle.git"
# Where the release tags are listed (unauthenticated).
TAGS_API="https://api.github.com/repos/diderisoft/bot-bottle/tags"
VENV="${BOT_BOTTLE_VENV:-${HOME}/.bot-bottle/venv}"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11
# The Python uv installs when the machine has no suitable one.
UV_PYTHON=3.13
# The uv release fetched when there's no Python and no uv: pinned, with its
# published SHA-256 per platform (musl on Linux: static, so it runs on NixOS).
UV_VERSION=0.12.24
UV_HOME="${HOME}/.bot-bottle/uv"

say() {
    printf 'bot-bottle install: %s\n' "$*" >&2
}

die() {
    say "error: $*"
    exit 1
}

# The package tool's own output (pip, uv, pipx) goes to a log, not the
# terminal: one line per step, a spinner on a terminal, and the log's tail
# when a step fails.
INSTALL_LOG="${HOME}/.bot-bottle/logs/install-$(date +%Y%m%d-%H%M%S).log"

run_logged() {
    label="$1"
    shift
    mkdir -p "$(dirname "${INSTALL_LOG}")"
    if [ -t 2 ]; then
        "$@" >>"${INSTALL_LOG}" 2>&1 &
        pid=$!
        i=0
        while kill -0 "${pid}" 2>/dev/null; do
            case $((i % 4)) in 0) c='|' ;; 1) c='/' ;; 2) c='-' ;; *) c='\' ;; esac
            printf '\rbot-bottle install: %s %s' "${label}" "${c}" >&2
            i=$((i + 1))
            sleep 0.2
        done
        if wait "${pid}"; then
            printf '\rbot-bottle install: %s ✓\n' "${label}" >&2
            return 0
        fi
        printf '\rbot-bottle install: %s ✗\n' "${label}" >&2
    else
        say "${label}"
        if "$@" >>"${INSTALL_LOG}" 2>&1; then
            return 0
        fi
    fi
    say "${label} failed; the end of ${INSTALL_LOG}:"
    tail -n 20 "${INSTALL_LOG}" | sed 's/^/    /' >&2
    return 1
}

# --- uninstall -----------------------------------------------------------------

if [ "${1:-}" = "--uninstall" ]; then
    shift
    bin=""
    for candidate in "$(command -v bot-bottle 2>/dev/null || true)" \
                     "${HOME}/.local/bin/bot-bottle" "${VENV}/bin/bot-bottle"; do
        if [ -n "${candidate}" ] && [ -x "${candidate}" ] && "${candidate}" --help >/dev/null 2>&1; then
            bin="${candidate}"
            break
        fi
    done
    if [ -n "${bin}" ]; then
        say "running '${bin} uninstall $*'"
        exec "${bin}" uninstall "$@"
    fi
    # No working command: remove what this script installs.
    say "bot-bottle isn't runnable; removing what the installer put here"
    if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^bot-bottle '; then
        uv tool uninstall bot-bottle
    fi
    if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q '^bot-bottle '; then
        pipx uninstall bot-bottle
    fi
    shim="${HOME}/.local/bin/bot-bottle"
    if [ -L "${shim}" ]; then
        rm -f "${shim}" && say "removed ${shim}"
    fi
    if [ -d "${VENV}" ]; then
        rm -rf "${VENV}" && say "removed ${VENV}"
    fi
    say "left in place: ~/.bot-bottle (your data), ~/.cache/bot-bottle, autostart,"
    say "backend setup, images and networks. Reinstall and run 'bot-bottle uninstall'"
    say "to remove those too."
    exit 0
fi

# --- which release ------------------------------------------------------------

if [ "${1:-}" = "--main" ]; then
    shift
    BOT_BOTTLE_INSTALL_REF=main
fi
[ "$#" -eq 0 ] || die "unknown argument '$1' (expected --main or --uninstall [--purge])"

# --- no bottled agents across the upgrade -----------------------------------------

# Upgrade bridge: remove once every host has upgraded past v0.1.9.
#
# Bottled agents are kept in the host database now, and nothing reads the
# files older versions kept them in (`state/<id>/metadata.json`). One running
# or kept for resume couldn't be resumed or cleaned up after the upgrade, so
# while any such file is left (the installed version's, in any environment)
# this refuses, before anything is replaced.
bottled_agents_left() {
    for root in "${BOT_BOTTLE_ROOT:-}" "${HOME}/.bot-bottle" "${HOME}"/.bot-bottle-envs/*; do
        [ -n "${root}" ] || continue
        for meta in "${root}"/state/*/metadata.json; do
            [ -f "${meta}" ] || continue
            dir="${meta%/metadata.json}"
            label="$(sed -n 's/^ *"label": *"\(.*\)",\{0,1\}$/\1/p' "${meta}" | head -n 1)"
            printf '    %s%s\n        %s\n' "${dir##*/}" "${label:+  (${label})}" "${dir}"
        done
    done
}
left="$(bottled_agents_left | awk '!seen[$0]++')"
if [ -n "${left}" ]; then
    say "error: this upgrade can't carry bottled agents across it, and these are still here:"
    printf '%s\n' "${left}" >&2
    say "stop each running one (exit its agent, or 'bot-bottle cleanup bots' stops them all),"
    say "and finish each kept for resume ('bot-bottle resume <id>', then exit it), or delete"
    say "its directory to discard it; in a named environment (~/.bot-bottle-envs/<name>),"
    say "run those with BOT_BOTTLE_ENV=<name>. Then run this installer again."
    exit 1
fi

# Print URL $1's body.
fetch_url() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O - "$1"
    else
        return 1
    fi
}

# The newest release tag (`vX.Y.Z`, by semver) from GitHub's tags API,
# reading every page. Fails when it can't be fetched or there is none.
latest_release_tag() {
    found=""
    page=1
    while [ "${page}" -le 20 ]; do
        body="$(fetch_url "${TAGS_API}?per_page=100&page=${page}")" || return 1
        printf '%s' "${body}" | grep -q '"name"' || break
        found="${found}
$(printf '%s' "${body}" \
    | grep -o '"name": *"v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*"' \
    | sed 's/.*"v/v/; s/"$//' || true)"
        page=$((page + 1))
    done
    latest="$(printf '%s\n' "${found}" | sed -n 's/^v//p' \
        | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)"
    [ -n "${latest}" ] || return 1
    echo "v${latest}"
}

# Only the default spec needs a ref; an explicit spec says its own.
INSTALL_REF=""
if [ -z "${BOT_BOTTLE_INSTALL_SPEC:-}" ]; then
    if [ -n "${BOT_BOTTLE_INSTALL_REF:-}" ]; then
        INSTALL_REF="${BOT_BOTTLE_INSTALL_REF}"
    else
        INSTALL_REF="$(latest_release_tag)" || die "couldn't find the latest bot-bottle release
  tag at ${TAGS_API} (it needs curl or wget, and network access).
  Retry, or pick what to install yourself:
    BOT_BOTTLE_INSTALL_REF=v0.1.4 sh install.sh    # a release tag
    sh install.sh --main                           # main; then 'bot-bottle up --build'"
    fi
    if [ "${INSTALL_REF}" = main ]; then
        say "installing main, not a release: its orchestrator and gateway images"
        say "aren't published, so build them with 'bot-bottle up --build' (needs Docker,"
        say "or Apple's container on macOS)"
    else
        say "installing bot-bottle ${INSTALL_REF}"
    fi
fi

if [ -n "${BOT_BOTTLE_ENV:-}" ]; then
    say "environment ${BOT_BOTTLE_ENV} (BOT_BOTTLE_ENV): its agents and state are what this sets up"
fi

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

# --- no suitable Python: let uv supply one ------------------------------------

# The uv build for this machine, and its published SHA-256.
uv_target() {
    case "$(uname -s)/$(uname -m)" in
        Darwin/arm64) echo "aarch64-apple-darwin 0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732" ;;
        Darwin/x86_64) echo "x86_64-apple-darwin 4fa82e37cb94767661f532b001e470b67a186c7260e305bd84ddb78fd545c0b6" ;;
        Linux/x86_64) echo "x86_64-unknown-linux-musl 48170bd200a5430298c18f3b264485a1f3a8605f01088277daf6e37633edf0f5" ;;
        Linux/aarch64|Linux/arm64) echo "aarch64-unknown-linux-musl 7f9ab4726d743b6a92e310477a7840c8a8a7b3938cb3c4657d541342fca75011" ;;
        *) return 1 ;;
    esac
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        return 1
    fi
}

# Fetch the pinned uv into ${UV_HOME}/bin, verify it, and print its path.
# Fails (returning 1) when this machine can't, so the caller can explain.
fetch_uv() {
    target_sum="$(uv_target)" || return 1
    target="${target_sum% *}"
    want="${target_sum#* }"
    url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz"
    tmp="$(mktemp -d)" || return 1
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "${tmp}/uv.tar.gz" "${url}" || { rm -rf "${tmp}"; return 1; }
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "${tmp}/uv.tar.gz" "${url}" || { rm -rf "${tmp}"; return 1; }
    else
        rm -rf "${tmp}"; return 1
    fi
    got="$(sha256_of "${tmp}/uv.tar.gz")" || { rm -rf "${tmp}"; return 1; }
    if [ "${got}" != "${want}" ]; then
        rm -rf "${tmp}"
        die "the downloaded uv ${UV_VERSION} (${target}) doesn't match its published checksum; not using it"
    fi
    tar -xzf "${tmp}/uv.tar.gz" -C "${tmp}" || { rm -rf "${tmp}"; return 1; }
    mkdir -p "${UV_HOME}/bin"
    mv "${tmp}/uv-${target}/uv" "${UV_HOME}/bin/uv"
    rm -rf "${tmp}"
    echo "${UV_HOME}/bin/uv"
}

# UV is set when uv installs bot-bottle on a Python it supplies; PRIVATE_UV
# when that uv is ours (its tools and Pythons kept under ${UV_HOME}).
UV=""
PRIVATE_UV=no
if [ -z "${PYTHON}" ]; then
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
        say "no Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ found; uv will install Python ${UV_PYTHON}"
    elif [ -x "${UV_HOME}/bin/uv" ] || { say "no Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ found; fetching uv ${UV_VERSION} to install Python ${UV_PYTHON}" && fetch_uv >/dev/null; }; then
        UV="${UV_HOME}/bin/uv"
        PRIVATE_UV=yes
    else
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
  ~/.local/bin, and python.org framework builds. Fetching uv to supply
  one failed too (it needs curl or wget, and sha256sum or shasum).

Install a newer Python, then re-run this installer:
${fix}

Already have one somewhere? Point at it directly:
  BOT_BOTTLE_PYTHON=/path/to/python3 sh install.sh"
    fi
fi

# Be explicit when the interpreter isn't the obvious one, so nobody is left
# wondering which Python their install ended up on.
path_python="$(command -v python3 2>/dev/null || true)"
if [ -n "${PYTHON}" ] && [ "${PYTHON}" != "${path_python}" ]; then
    say "using ${PYTHON} ($(python_version "${PYTHON}"))"
    if [ -n "${path_python}" ]; then
        say "note: 'python3' on your PATH is ${path_python} ($(python_version "${path_python}")), which is below the ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} floor"
    fi
fi

# Pairing a phone (remote access) needs the iroh bindings: the `remote` extra.
# iroh publishes no Intel-macOS wheel, so there the package installs without
# it. Decided by the interpreter the install uses, which is what pip matches
# wheels against (and stays right under Rosetta).
REMOTE=yes
if [ "$(uname -s)" = Darwin ]; then
    if [ -n "${PYTHON}" ]; then
        machine="$("${PYTHON}" -c 'import platform; print(platform.machine())')"
    else
        machine="$(uname -m)"  # uv's Python matches the uv build we picked
    fi
    [ "${machine}" = x86_64 ] && REMOTE=no
fi
if [ "${REMOTE}" = yes ]; then
    PACKAGE_SPEC="${BOT_BOTTLE_INSTALL_SPEC:-bot-bottle[remote] @ ${REPO_SPEC}@${INSTALL_REF}}"
else
    PACKAGE_SPEC="${BOT_BOTTLE_INSTALL_SPEC:-${REPO_SPEC}@${INSTALL_REF}}"
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

# A Python without its venv module (Debian and Ubuntu ship that separately,
# as python3-venv) can't make the private venv below, and pipx needs it too.
# uv makes its own environments: fetch ours and install onto that Python.
if [ -z "${UV}" ] && ! command -v uv >/dev/null 2>&1 && ! command -v pipx >/dev/null 2>&1 \
    && ! "${PYTHON}" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    if [ -x "${UV_HOME}/bin/uv" ] || { say "${PYTHON} has no venv module; fetching uv ${UV_VERSION} to install with" && fetch_uv >/dev/null; }; then
        UV="${UV_HOME}/bin/uv"
        PRIVATE_UV=yes
        UV_PYTHON="${PYTHON}"
    fi
fi

# --- config directories ------------------------------------------------------

mkdir -p \
    "${HOME}/.bot-bottle/agents" \
    "${HOME}/.bot-bottle/bottles" \
    "${HOME}/.bot-bottle/contrib"

# --- stop an older version's host controller ---------------------------------

# Upgrade bridge: remove once every host has upgraded past v0.1.9.
#
# Up to v0.1.9 the host controller published itself in `hostctl/state.json`.
# The new CLI finds one only on `hostctl/control.sock`, so it wouldn't see an
# old one still running and would start a second beside it (or, with a service
# unit, wait on a unit that's already up). The check above left no bottled
# agents, so stopping it loses nothing. An old daemon removes state.json when
# it stops, so this does nothing once it's gone.
old_controller_roots() {
    for root in "${BOT_BOTTLE_ROOT:-}" "${HOME}/.bot-bottle" "${HOME}"/.bot-bottle-envs/*; do
        [ -n "${root}" ] && [ -f "${root}/hostctl/state.json" ] && printf '%s\n' "${root}"
    done
    return 0
}
stop_old_host_controllers() {
    roots="$(old_controller_roots | awk '!seen[$0]++')"
    [ -n "${roots}" ] || return 0
    # The old CLI's own `down`, for the environment this installer runs in.
    old_bin="$(command -v bot-bottle 2>/dev/null || true)"
    if [ -n "${old_bin}" ]; then
        "${old_bin}" down </dev/null >/dev/null 2>&1 || true
    fi
    printf '%s\n' "${roots}" | while IFS= read -r root; do
        # Its service unit, if it has one, so the manager doesn't start it again.
        case "${root}" in
            "${HOME}/.bot-bottle") unit=bot-bottle-hostctl.service label=io.bot-bottle.hostctl ;;
            "${HOME}"/.bot-bottle-envs/*)
                name="${root##*/}"
                unit="bb-${name}-hostctl.service" label="io.bot-bottle.${name}.hostctl" ;;
            *) unit="" label="" ;;
        esac
        if [ -n "${unit}" ] && [ -f "${HOME}/.config/systemd/user/${unit}" ] \
            && command -v systemctl >/dev/null 2>&1; then
            systemctl --user stop "${unit}" >/dev/null 2>&1 || true
        fi
        if [ -n "${label}" ] && [ -f "${HOME}/Library/LaunchAgents/${label}.plist" ] \
            && command -v launchctl >/dev/null 2>&1; then
            launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
        fi
        # And the process state.json names, if it's still a host controller
        # (not a reused pid).
        [ -f "${root}/hostctl/state.json" ] || continue
        pid="$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "${root}/hostctl/state.json")"
        [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null || continue
        ps -p "${pid}" -o args= 2>/dev/null | grep -q 'bot_bottle\.control\.hostctl' || continue
        say "stopping the older version's host controller (pid ${pid}, ${root})"
        kill "${pid}" 2>/dev/null || true
        i=0
        while kill -0 "${pid}" 2>/dev/null && [ "${i}" -lt 50 ]; do
            sleep 0.2
            i=$((i + 1))
        done
        if kill -0 "${pid}" 2>/dev/null; then
            say "warning: the older host controller (pid ${pid}) didn't stop; stop it"
            say "before running bot-bottle"
        fi
    done
}
stop_old_host_controllers

# --- install -----------------------------------------------------------------

BIN_DIR="${HOME}/.local/bin"

if [ -n "${UV}" ]; then
    # No suitable Python: uv installs one. Ours keeps its tools and Pythons
    # under ${UV_HOME}, so `bot-bottle uninstall` removes them all and a uv of
    # the user's never sees them.
    if [ "${PRIVATE_UV}" = yes ]; then
        export UV_TOOL_DIR="${UV_HOME}/tools" UV_PYTHON_INSTALL_DIR="${UV_HOME}/python"
        export UV_TOOL_BIN_DIR="${BIN_DIR}"
    fi
    run_logged "installing with uv on Python ${UV_PYTHON}" \
        "${UV}" tool install --python "${UV_PYTHON}" --force "${PACKAGE_SPEC}" \
        || die "uv couldn't install bot-bottle (log: ${INSTALL_LOG})"
    uv_bin="$("${UV}" tool dir --bin 2>/dev/null || true)"
    [ -n "${uv_bin}" ] && BIN_DIR="${uv_bin}"
elif command -v uv >/dev/null 2>&1; then
    # uv first: fastest, and someone with uv manages their Python tools with
    # it. --python pins the tool's venv to the interpreter we vetted, as for
    # pipx below.
    run_logged "installing with uv (python: ${PYTHON})" \
        uv tool install --python "${PYTHON}" --force "${PACKAGE_SPEC}" \
        || die "uv couldn't install bot-bottle (log: ${INSTALL_LOG})"
    # Ask uv where it puts entry points rather than assuming ~/.local/bin.
    uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
    [ -n "${uv_bin}" ] && BIN_DIR="${uv_bin}"
elif command -v pipx >/dev/null 2>&1; then
    # --python pins the venv to the interpreter we vetted. Without it pipx uses
    # whichever Python it was itself installed with, which is not necessarily
    # the one that passed the version check above.
    run_logged "installing with pipx (python: ${PYTHON})" \
        pipx install --python "${PYTHON}" --force "${PACKAGE_SPEC}" \
        || die "pipx couldn't install bot-bottle (log: ${INSTALL_LOG})"
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
    run_logged "installing bot-bottle and its dependencies" \
        "${VENV}/bin/python" -m pip install --upgrade "${PACKAGE_SPEC}" \
        || die "pip couldn't install bot-bottle (log: ${INSTALL_LOG})"
    # Expose the entry point outside the venv, the way pipx would.
    mkdir -p "${BIN_DIR}"
    ln -sf "${VENV}/bin/bot-bottle" "${BIN_DIR}/bot-bottle"
fi

# --- locate the entry point --------------------------------------------------

# Always run the entry point just installed: another bot-bottle earlier on
# PATH (an older install) would otherwise run setup and doctor instead.
on_path="$(command -v bot-bottle 2>/dev/null || true)"
if [ -x "${BIN_DIR}/bot-bottle" ] && [ "${on_path}" = "${BIN_DIR}/bot-bottle" ]; then
    BOT_BOTTLE_BIN="bot-bottle"
elif [ -x "${BIN_DIR}/bot-bottle" ] && [ -n "${on_path}" ]; then
    BOT_BOTTLE_BIN="${BIN_DIR}/bot-bottle"
    say "note: 'bot-bottle' on your PATH is ${on_path}, not this install"
    say "      (${BOT_BOTTLE_BIN}); put ${BIN_DIR} first, or remove the other one"
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
elif [ -n "${on_path}" ]; then
    # Installed somewhere other than BIN_DIR (a custom spec): the one on PATH.
    BOT_BOTTLE_BIN="bot-bottle"
else
    die "bot-bottle was installed but no entry point turned up in ${BIN_DIR}"
fi

if [ "${REMOTE}" = no ]; then
    say "note: this is an Intel Mac, which iroh has no build for: pairing a phone"
    say "(remote access) isn't available here. Everything else is."
fi

# --- host setup, starters, check ----------------------------------------------

# `bootstrap` is install, setup, then doctor. Under `curl | sh` stdin is this
# script, so their prompts (the one sudo step, OpenRouter sign-in, pairing)
# read from the terminal instead, when there is one; without one, install
# prints the sudo step for you to run. doctor ends with the one next step for
# this host (`next: ...`), whatever it found.
say "setting up this host ('${BOT_BOTTLE_BIN} bootstrap')"
if [ -t 1 ] && (: </dev/tty) 2>/dev/null; then
    "${BOT_BOTTLE_BIN}" bootstrap </dev/tty && done=yes || done=no
else
    "${BOT_BOTTLE_BIN}" bootstrap </dev/null && done=yes || done=no
fi
if [ "${done}" = yes ]; then
    say "installed."
else
    say "installed, but setting up this host didn't finish."
    printf "\nFix what it says above, then run '%s bootstrap' again:\n\n    %s bootstrap\n\n" \
        "${BOT_BOTTLE_BIN}" "${BOT_BOTTLE_BIN}" >&2
fi
