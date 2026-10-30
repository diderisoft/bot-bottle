"""The guest's git hooks that add the `Bot-Bottle` commit trailer.

The agent's env sets `core.hooksPath` through `GIT_CONFIG_COUNT`, which
overrides a repo's own `core.hooksPath` (bot-bottle's `.githooks`, say), to a
provisioned directory holding one dispatcher under every hook name. The
dispatcher runs the hook git would have run without the override (the
repo's `core.hooksPath`, else `<common dir>/hooks/<name>`) with the same
args, stdin and exit code. As `prepare-commit-msg` it then adds the trailers
with `git interpret-trailers`, never twice:

    Bot-Bottle: <bottle>/<run>
    Co-authored-by: <operator>     # when known and not the commit's author

The trailer values are in `bot-bottle.gitconfig` next to the hooks.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping

from ..contract.git_trailers import TRAILER_KEY

HOOKS_SUBDIR = ".config/git/bot-bottle-hooks"
DISPATCHER_NAME = "bot-bottle-hook"
CONFIG_NAME = "bot-bottle.gitconfig"

# githooks(5). Left out: push-to-checkout and proc-receive, whose mere
# presence changes what git does, and fsmonitor-watchman, which git finds
# through core.fsmonitor rather than the hooks directory.
HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch",
    "pre-commit", "pre-merge-commit", "prepare-commit-msg", "commit-msg",
    "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push",
    "pre-receive", "update", "post-receive", "post-update",
    "reference-transaction", "pre-auto-gc", "post-rewrite",
    "sendemail-validate", "p4-changelist", "p4-prepare-changelist",
    "p4-post-changelist", "p4-pre-submit", "post-index-change",
)


def hooks_dir(guest_home: str) -> str:
    return f"{guest_home.rstrip('/')}/{HOOKS_SUBDIR}"


def hooks_env(guest_home: str, guest_env: Mapping[str, str]) -> dict[str, str]:
    """The env entries that point `core.hooksPath` at the provisioned hooks,
    appended after any `GIT_CONFIG_*` entries `guest_env` already has."""
    try:
        index = int(guest_env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        index = 0
    return {
        "GIT_CONFIG_COUNT": str(index + 1),
        f"GIT_CONFIG_KEY_{index}": "core.hooksPath",
        f"GIT_CONFIG_VALUE_{index}": hooks_dir(guest_home),
    }


def operator_identity(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str]:
    """The operator's (name, email): the host's global git identity, or
    ("", "") when either is unset or git can't be run."""
    values: list[str] = []
    for key in ("user.name", "user.email"):
        try:
            proc = run(["git", "config", "--global", "--get", key],
                       capture_output=True, text=True, check=False, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return "", ""
        value = proc.stdout.strip() if proc.returncode == 0 else ""
        if not value:
            return "", ""
        values.append(value)
    return values[0], values[1]


def _gitconfig_quote(field: str, value: str) -> str:
    if any(c in value for c in "\n\r\0<>"):
        raise ValueError(f"commit hooks: {field} {value!r} has a character "
                         "a git identity can't hold")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_hook_config(trailer: str, operator_name: str = "",
                       operator_email: str = "") -> str:
    """`bot-bottle.gitconfig`: the trailer value and, when both are known,
    the operator to credit as co-author."""
    lines = ["[botBottle]", f"\ttrailer = {_gitconfig_quote('trailer', trailer)}"]
    if operator_name and operator_email:
        lines.append(f"\toperatorName = {_gitconfig_quote('operator name', operator_name)}")
        lines.append(f"\toperatorEmail = {_gitconfig_quote('operator email', operator_email)}")
    return "\n".join(lines) + "\n"


def render_install(directory: str, owner: str = "node:node") -> str:
    """POSIX sh, run as root once the dispatcher and config are copied into
    `directory`: link every hook name to the dispatcher and hand the
    directory to the agent's user."""
    links = " ".join(HOOK_NAMES)
    return (
        f"set -e; cd '{directory}'; chmod 755 {DISPATCHER_NAME}; "
        f"chmod 644 {CONFIG_NAME}; "
        f"for h in {links}; do ln -sf {DISPATCHER_NAME} \"$h\"; done; "
        f"chown -R {owner} '{directory}'"
    )


def render_dispatcher() -> str:
    """The hook dispatcher (POSIX sh), linked under every hook name."""
    return r"""#!/bin/sh
# bot-bottle git hook dispatcher. core.hooksPath points here (set through
# GIT_CONFIG_COUNT in the agent's env); every hook name links to this file.
# It runs the repo's own hook, then, as prepare-commit-msg, adds the
# Bot-Bottle trailer. Provisioned by bot-bottle; edits are lost on launch.
name=${0##*/}
here=$(cd "${0%/*}" && pwd -P)

# The hook git would run without our override: the hooksPath from the
# repo's own (and the user's) config, else <common dir>/hooks.
repo_hook() {
  dir=$(unset GIT_CONFIG_COUNT; git config --type=path --get core.hooksPath 2>/dev/null)
  if [ -z "$dir" ]; then
    common=$(git rev-parse --git-common-dir 2>/dev/null) || return 1
    dir=$common/hooks
  fi
  [ -d "$dir" ] || return 1
  [ "$(cd "$dir" && pwd -P)" = "$here" ] && return 1
  [ -f "$dir/$name" ] && [ -x "$dir/$name" ] || return 1
  printf '%s\n' "$dir/$name"
}

if [ "$name" != prepare-commit-msg ]; then
  hook=$(repo_hook) || exit 0
  exec "$hook" "$@"
fi

if hook=$(repo_hook); then
  "$hook" "$@" || exit $?
fi

msg=$1
cfg=$here/bot-bottle.gitconfig
trailer=$(git config --file "$cfg" --get botBottle.trailer 2>/dev/null)
if [ -z "$trailer" ] || [ -z "$msg" ]; then
  exit 0
fi
set -- --if-exists replace --trailer "TRAILER_KEY: $trailer"
op_name=$(git config --file "$cfg" --get botBottle.operatorName 2>/dev/null)
op_email=$(git config --file "$cfg" --get botBottle.operatorEmail 2>/dev/null)
if [ -n "$op_name" ] && [ -n "$op_email" ]; then
  author=$(git var GIT_AUTHOR_IDENT 2>/dev/null | sed -n 's/^.*<\(.*\)>.*$/\1/p')
  lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
  if [ "$(lower "$author")" != "$(lower "$op_email")" ]; then
    set -- "$@" --if-exists addIfDifferent \
      --trailer "Co-authored-by: $op_name <$op_email>"
  fi
fi
if ! git interpret-trailers --in-place "$@" "$msg"; then
  echo "bot-bottle: could not add the TRAILER_KEY trailer; git-gate may refuse this commit" >&2
fi
exit 0
""".replace("TRAILER_KEY", TRAILER_KEY)


__all__ = [
    "CONFIG_NAME", "DISPATCHER_NAME", "HOOKS_SUBDIR", "HOOK_NAMES", "hooks_dir",
    "hooks_env", "operator_identity", "render_dispatcher", "render_hook_config",
    "render_install",
]
