"""The CLI side of a launch (PRD 0092): resolve what only the CLI can (secret
values, confirmed host keys, yes/no answers), send the host controller a
`LaunchRequest`, stream the job, then attach to the agent's tmux session."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace

from ..contract.hostctl import (
    BOTTLES_PATH, CONFIRM_ERROR, SESSIONS_PATH, LaunchRequest, exit_code,
)
from ..control.hostctl import runner
from ..control.hostctl.client import HostControlClient
from ..control.hostctl.sessions import SessionManager
from ..env import env_entry_interpolated_from, env_entry_kind, prompt_secret
from ..git_gate.host_key import preflight_host_keys
from ..lib import log
from ..lib.log import die, info, warn
from ..manifest import Manifest, ManifestIndex
from ..secret_source import SecretSource
from ..util import read_tty_line
from .constants import PROG
from .secrets_env import secrets_from_env

# How long to wait for a session that ended to report its exit.
EXIT_REPORT_SECONDS = 5.0


def needed_secrets(manifest: Manifest) -> dict[str, str]:
    """Every secret name `manifest` resolves at launch, mapped to its `?`
    env entry when one may be prompted for, else to ""."""
    bottle = manifest.bottle
    names: dict[str, str] = {}
    for name, raw in bottle.env.items():
        kind = env_entry_kind(raw)
        if kind == "secret" and name:
            names[name] = raw
        elif kind == "interpolated":
            names.setdefault(env_entry_interpolated_from(raw), "")
    for route in bottle.egress.routes:
        if route.TokenRef:
            names.setdefault(route.TokenRef, "")
    for entry in bottle.git:
        if entry.Key.forge_token_env:
            names.setdefault(entry.Key.forge_token_env, "")
    return names


def resolve_secrets(manifest: Manifest, source: SecretSource, *, prompt: bool) -> dict[str, str]:
    """The values of `manifest`'s secrets that `source` has, plus (when
    `prompt`) a typed-in value for each `?` entry it doesn't. Missing ones
    are left for the launch to report."""
    values: dict[str, str] = {}
    for name, raw in needed_secrets(manifest).items():
        value = source.get(name)
        if value:
            values[name] = value
        elif raw and prompt:
            values[name] = prompt_secret(name, raw)
    return values


def confirm_host_keys(manifest: Manifest, index: ManifestIndex) -> dict[str, str]:
    """Fetch and confirm (on the tty) each upstream host key the manifest
    lacks; the confirmed keys by repo name."""
    missing = {e.Name for e in manifest.bottle.git if not e.KnownHostKey}
    if not missing:
        return {}
    updated = preflight_host_keys(manifest, headless=False, home_md=index.home_md)
    return {e.Name: e.KnownHostKey for e in updated.bottle.git
            if e.Name in missing and e.KnownHostKey}


def prepare_request(request: LaunchRequest, index: ManifestIndex, *,
                    interactive: bool) -> LaunchRequest:
    """`request` with the secrets it needs and, when `interactive`, the
    host keys the user confirmed."""
    manifest = index.load_for_agent(request.agent, request.bottles)
    host_keys = confirm_host_keys(manifest, index) if interactive else {}
    secrets = resolve_secrets(manifest, secrets_from_env(), prompt=interactive)
    return replace(request, secrets=secrets, host_keys=host_keys)


def _prompt_yes(question: str) -> bool:
    sys.stderr.write(f"bot-bottle: {question} [y/N] ")
    sys.stderr.flush()
    return read_tty_line() in ("y", "Y", "yes", "YES")


def launch(request: LaunchRequest) -> int:
    """Launch through the host controller and attach. Returns the agent's
    exit code, or 0 if the user detached or declined."""
    try:
        client = HostControlClient(runner.ensure_running(), runner.cli_token())
        while True:
            job = str(client.post(BOTTLES_PATH, request.to_json())["job"])
            final = client.follow(job, log.write_line)
            if final is None:
                die(f"detached; the launch continues as job {job} (see `{PROG} hostctl jobs`)")
            if final.get("type") != "error":
                break
            message = str(final.get("message") or "launch failed")
            if final.get("code") != CONFIRM_ERROR or request.assume_yes:
                die(message)
            if not _prompt_yes(message):
                info("aborted by user")
                return 0
            request = replace(request, assume_yes=True)
        value = final.get("value")
        if not isinstance(value, dict) or not isinstance(value.get("session"), str):
            die("host controller returned no session")
        return attach(client, value["session"])
    except runner.HostControlError as e:
        die(str(e))


def tmux_attach(session_id: str) -> tuple[list[str], dict[str, str]]:
    """The writable local `tmux attach` command for `session_id` and its
    env. `TMUX` is dropped: bot-bottle's server is separate, so attaching
    from inside the user's own tmux isn't nesting."""
    tmux_bin = shutil.which("tmux")
    if tmux_bin is None:
        die("tmux is not installed")
    argv = SessionManager({}).attach_argv(session_id, read_only=False)
    argv[0] = tmux_bin
    return argv, {k: v for k, v in os.environ.items() if k != "TMUX"}


def attach(client: HostControlClient, session_id: str) -> int:
    """Attach this terminal to `session_id` until it ends or the user
    detaches; the agent's exit code, or 0 after a detach."""
    info(f"attaching to {session_id} (Ctrl-] detaches; the bottle is removed "
         "when the agent exits)")
    argv, env = tmux_attach(session_id)
    subprocess.run(argv, env=env, check=False)
    deadline = time.monotonic() + EXIT_REPORT_SECONDS
    while True:
        state = client.get(f"{SESSIONS_PATH}/{session_id}")
        ended = state.get("exit")
        if isinstance(ended, dict):
            code = exit_code(int(ended.get("status") or 0), str(ended.get("signal") or ""))
            info(f"session ended (exit {code})")
            return code
        if state.get("running"):
            info(f"detached; the agent keeps running. Reattach with `{PROG} attach {session_id}`")
            return 0
        if time.monotonic() > deadline:
            warn(f"session {session_id} ended without reporting how")
            return 1
        time.sleep(0.1)


__all__ = [
    "attach", "confirm_host_keys", "launch", "needed_secrets", "prepare_request",
    "resolve_secrets", "tmux_attach",
]
