"""The CLI side of a launch: resolve what only the CLI can (secrets, confirmed
host keys, answers), send a `LaunchRequest`, stream the job, then attach to the
agent's tmux session."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

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
from ..lib.prompt import current as current_prompter
from ..manifest import Manifest, ManifestIndex, trust
from ..secret_source import SecretSource
from ..util import read_tty_line
from ..workspace import patch_summary, work_patch_path
from .constants import PROG
from .progress import LaunchProgress, launch_log_path

if TYPE_CHECKING:
    from .screen import Screen, Step
from .secrets_env import secrets_from_env

# How long a finished session gets to report its exit.
EXIT_REPORT_SECONDS = 5.0
# How long a headless launch without a terminal watches for an agent that
# fails at once, before leaving it running.
HEADLESS_WATCH_SECONDS = 5.0


def needed_secrets(manifest: Manifest) -> dict[str, str]:
    """Every secret name `manifest` resolves at launch, mapped to its `?` env
    entry when it can be prompted for, else ""."""
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
    # The provider's egress routes take their tokens from these names.
    provider = manifest.agent.agent_provider
    for name in (provider.auth_token, str(provider.settings.get("api_key_env", ""))):
        if name:
            names.setdefault(name, "")
    return names


def resolve_secrets(manifest: Manifest, source: SecretSource, *, prompt: bool) -> dict[str, str]:
    """The values `source` has for `manifest`'s secrets, plus (with `prompt`) a
    typed value for each missing `?` entry. Others are left for the launch to
    report."""
    values: dict[str, str] = {}
    for name, raw in needed_secrets(manifest).items():
        value = source.get(name)
        if value:
            values[name] = value
        elif raw and prompt:
            values[name] = prompt_secret(name, raw)
    return values


def confirm_host_keys(manifest: Manifest, index: ManifestIndex) -> dict[str, str]:
    """Fetch and confirm on the tty each upstream host key the manifest lacks;
    returns them by repo name."""
    missing = {e.Name for e in manifest.bottle.git if not e.KnownHostKey}
    if not missing:
        return {}
    updated = preflight_host_keys(manifest, headless=False, home_md=index.home_md)
    return {e.Name: e.KnownHostKey for e in updated.bottle.git
            if e.Name in missing and e.KnownHostKey}


def prepare_request(request: LaunchRequest, index: ManifestIndex, *,
                    interactive: bool) -> LaunchRequest:
    """`request` with its secrets and, when `interactive`, the confirmed host
    keys."""
    manifest = load_trusting(index, request.agent, request.bottles, interactive=interactive)
    host_keys = confirm_host_keys(manifest, index) if interactive else {}
    secrets = resolve_secrets(manifest, secrets_from_env(), prompt=interactive)
    return replace(request, secrets=secrets, host_keys=host_keys)


def load_trusting(index: ManifestIndex, agent: str, bottles: tuple[str, ...], *,
                  interactive: bool) -> Manifest:
    """Load the agent's manifest. An untrusted or changed project bottle (or
    project agent with a provider) is
    shown and asked about first; without a terminal it fails with a pointer to
    `bot-bottle trust`. Declining stops the launch."""
    while True:
        try:
            return index.load_for_agent(agent, bottles)
        except trust.UntrustedBottleError as e:
            if not interactive:
                raise
            if not _trusts(e):
                die(f"not started; the {e.kind} stays untrusted "
                    f"(`{PROG} trust {e.bottle}` to review it later)")
            trust.trust(e)
            info(f"trusted {e.bottle} on this host")


def _trusts(error: trust.UntrustedBottleError) -> bool:
    """Ask whether to trust the bottle (or agent) `error` names."""
    asker = current_prompter()
    if asker is not None:
        lines, question = trust.prompt_parts(error)
        return asker.confirm(question, context=lines)
    sys.stderr.write("\n" + trust.prompt_text(error))
    sys.stderr.flush()
    return read_tty_line() in ("y", "Y", "yes", "YES")


def _prompt_yes(question: str, context: tuple[str, ...] = ()) -> bool:
    asker = current_prompter()
    if asker is not None:
        return asker.confirm(question, context=context)
    for line in context:
        sys.stderr.write(f"bot-bottle: {line}\n")
    sys.stderr.write(f"bot-bottle: {question} [y/N] ")
    sys.stderr.flush()
    return read_tty_line() in ("y", "Y", "yes", "YES")


def confirm_controller_restart(held: list[str]) -> bool:
    """Ask before restarting an updated host controller that holds bottles.
    Without a terminal there's no one to ask, so no."""
    if not sys.stdin.isatty():
        return False
    return _prompt_yes("Continue?", (
        f"bot-bottle was updated. Restarting the host controller moves "
        f"{len(held)} running bottle(s) ({', '.join(held)}) to the new version: the agents "
        "keep running, but attached terminals and the phone link disconnect briefly.",))


def _never(held: list[str]) -> bool:
    del held
    return False


def launch(request: LaunchRequest, *, verbose: bool = False, view: "Screen | None" = None,
           lead: "list[Step] | None" = None) -> int:
    """Launch through the host controller and attach. Returns the agent's exit
    code, or 0 on detach or decline. One line per stage; the full log goes to a
    file (`verbose` streams it). `view` and `lead`: draw the stages on a guided
    start's screen, under its steps."""
    progress = LaunchProgress(launch_log_path(request.label or request.agent), verbose=verbose,
                              view=view, lead=lead)
    try:
        confirm = _never if request.headless else confirm_controller_restart
        client = HostControlClient(runner.ensure_running(confirm_restart=confirm),
                                   runner.cli_token())
        while True:
            job = str(client.post(BOTTLES_PATH, request.to_json())["job"])
            final = client.follow(job, progress.line, progress.stage)
            if final is None:
                progress.close()
                die(f"detached; the launch continues as job {job} (see `{PROG} jobs`)")
            if final.get("type") != "error":
                break
            message = str(final.get("message") or "launch failed")
            if final.get("code") != CONFIRM_ERROR or request.assume_yes:
                progress.fail(f"{PROG}: {message}")
                raise log.Die(1, message)
            progress.pause()
            if not verbose:
                # The preflight summary the job logged, for confirmation.
                for line in progress.since_stage():
                    sys.stderr.write(line + "\n")
            if not _prompt_yes(message):
                progress.close()
                info("aborted by user")
                return 0
            request = replace(request, assume_yes=True)
        value = final.get("value")
        if not isinstance(value, dict) or not isinstance(value.get("session"), str):
            progress.fail(f"{PROG}: host controller returned no session")
            raise log.Die(1, "host controller returned no session")
        progress.done(f"{request.label or request.agent} is running")
        if request.headless and not _stdin_is_tty():
            return report_headless(client, value["session"])
        started = time.time()
        code = attach(client, value["session"])
        _report_work(value["session"], since=started)
        return code
    except runner.HostControlError as e:
        progress.close()
        die(str(e))


def tmux_attach(session_id: str) -> tuple[list[str], dict[str, str]]:
    """The writable `tmux attach` command for `session_id` and its env. `TMUX`
    is dropped (bot-bottle's server is separate); a missing or `dumb` TERM
    becomes `xterm-256color`."""
    tmux_bin = shutil.which("tmux")
    if tmux_bin is None:
        die("tmux is not installed")
    argv = SessionManager({}).attach_argv(session_id, read_only=False)
    argv[0] = tmux_bin
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    # tmux won't attach to a terminal it can't drive.
    if env.get("TERM", "dumb") == "dumb":
        env["TERM"] = "xterm-256color"
    return argv, env


def _report_work(session_id: str, *, since: float) -> None:
    """After the agent exits: what it changed and where the patch is."""
    path = work_patch_path(session_id)
    try:
        if path.stat().st_mtime < since:
            return
        patch = path.read_text(encoding="utf-8")
    except OSError:
        return
    files, added, removed = patch_summary(patch)
    try:
        shown = f"~/{path.relative_to(Path.home())}"
    except ValueError:
        shown = str(path)
    sys.stderr.write(
        f"\n{session_id} changed {files} file{'s' if files != 1 else ''} "
        f"(+{added} −{removed}). The changes are saved as a patch:\n  {shown}\n"
        f"Apply them here with:  git apply {shown}\n")


def _stdin_is_tty() -> bool:
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError):
        return False


def _ended(state: dict[str, object]) -> int | None:
    """The agent's exit code if `state` says the session ended, else None.
    A failure's last output goes to stderr."""
    ended = state.get("exit")
    if not isinstance(ended, dict):
        return None
    code = exit_code(int(ended.get("status") or 0), str(ended.get("signal") or ""))
    last = str(ended.get("last_output") or "")
    if code and last:
        sys.stderr.write(f"bot-bottle: its last output:\n{last}\n")
    return code


def report_headless(client: HostControlClient, session_id: str, *,
                    watch: float = HEADLESS_WATCH_SECONDS) -> int:
    """A headless launch with no terminal to attach: watch `watch` seconds
    for an agent that ends at once (and say why), then report the session and
    leave it running. The session id goes to stdout, for scripts."""
    deadline = time.monotonic() + watch
    while time.monotonic() < deadline:
        code = _ended(client.get(f"{SESSIONS_PATH}/{session_id}"))
        if code is not None:
            info(f"{session_id} ended at once (exit {code})")
            print(session_id)
            return code
        time.sleep(0.25)
    info(f"{session_id} is running headless; watch it with `{PROG} attach {session_id}`. "
         "When it exits, its changes are saved as a patch")
    print(session_id)
    return 0


def attach(client: HostControlClient, session_id: str) -> int:
    """Attach to `session_id` until it ends or the user detaches. Returns the
    agent's exit code, or 0 after a detach."""
    info(f"attaching to {session_id} (Ctrl-] detaches; the bottle is removed "
         "when the agent exits)")
    argv, env = tmux_attach(session_id)
    subprocess.run(argv, env=env, check=False)
    deadline = time.monotonic() + EXIT_REPORT_SECONDS
    while True:
        state = client.get(f"{SESSIONS_PATH}/{session_id}")
        code = _ended(state)
        if code is not None:
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
    "attach", "report_headless", "confirm_host_keys", "launch", "needed_secrets", "prepare_request",
    "resolve_secrets", "tmux_attach",
]
