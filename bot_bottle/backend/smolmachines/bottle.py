"""SmolmachinesBottle — running-instance handle (PRD 0023 chunk 2d).

Routes `exec_claude` / `exec` / `cp_in` through `smolvm machine
exec` / `smolvm machine cp`. The handle is yielded by `launch`
and torn down via the surrounding ExitStack on context exit;
`close` is a no-op idempotent alias so the BottleBackend ABC's
context-manager contract is satisfied.

User context: `smolvm machine exec` runs commands as root in the
VM, but the agent image's USER is `node` and claude-code refuses
to run as root with `--dangerously-skip-permissions`. Both
`exec_claude` and `exec` switch to the requested user (default
`node`) via `runuser -u <user> --` and set `HOME` / `USER`
through `smolvm -e` — avoiding `runuser -l`'s login-shell wiring
(PAM session setup, /etc/profile sourcing) which can hang on a
minimal Debian VM with no PAM session config."""

from __future__ import annotations

import subprocess
import sys
from typing import Mapping

from ...agent_provider import prompt_args
from .. import Bottle, ExecResult
from . import pty_resize as _pty_resize
from . import smolvm as _smolvm


# Absolute path to the pty_resize wrapper. Invoke as
# `python <path>` rather than `python -m <dotted-path>` so the
# wrapper runs regardless of cwd / sys.path — it has no
# bot_bottle.* imports, so it's self-contained.
_PTY_RESIZE_SCRIPT = _pty_resize.__file__


# Per-user env the agent image's USER (node) expects. claude
# reads ~/.claude.json + writes session state under ~/.claude/;
# bare `runuser -u` inherits root's HOME=/root, which claude
# can't write to. Set HOME / USER explicitly through smolvm -e
# so the child process sees them.
_HOME_FOR = {
    "node": "/home/node",
    "root": "/root",
}


def _env_flags_for(user: str) -> list[str]:
    home = _HOME_FOR.get(user, f"/home/{user}")
    return ["-e", f"HOME={home}", "-e", f"USER={user}"]


def _guest_env_flags(env: Mapping[str, str]) -> list[str]:
    """Render `{K: V}` into a flat `-e K=V` argv slice for
    `smolvm machine exec`. `smolvm machine create -e` set env
    on PID 1 but it doesn't propagate to fresh exec process
    trees, so we have to re-pass them every call."""
    out: list[str] = []
    for k, v in env.items():
        out += ["-e", f"{k}={v}"]
    return out


class SmolmachinesBottle(Bottle):
    """Handle returned by `SmolmachinesBottleBackend.launch`. The
    underlying VM lifecycle (create / start / stop / delete) lives
    on the launch ExitStack — this class only routes runtime
    operations to the right `smolvm machine ...` subcommand."""

    def __init__(
        self,
        machine_name: str,
        *,
        prompt_path: str | None = None,
        guest_env: Mapping[str, str] | None = None,
        agent_command: str = "claude",
        agent_prompt_mode: str = "claude_append_file",
    ) -> None:
        self.name = machine_name
        # In-VM path to the agent's prompt file. None when the
        # agent declared no prompt (file still exists; we just
        # don't pass --append-system-prompt-file).
        self._prompt_path = prompt_path
        # Env vars the agent process needs (HTTPS_PROXY,
        # CLAUDE_CODE_OAUTH_TOKEN, manifest-declared bottle env, …).
        # Forwarded on every `smolvm machine exec` via `-e K=V`
        # because exec doesn't inherit from machine_create's env.
        self._guest_env = dict(guest_env or {})
        self._agent_command = agent_command
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_command = agent_command
        self.agent_provider_template = (
            "codex" if agent_command == "codex" else "claude"
        )

    def claude_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        flags = ["smolvm", "machine", "exec", "--name", self.name]
        if tty:
            flags += ["-i", "-t"]
        flags += _env_flags_for("node")
        flags += _guest_env_flags(self._guest_env)
        claude_tail = [self._agent_command]
        provider_prompt_args = prompt_args(
            self._agent_prompt_mode, self._prompt_path, argv=argv,
        )
        if self._agent_prompt_mode == "codex_read_prompt_file":
            claude_tail += argv
            claude_tail += provider_prompt_args
        else:
            claude_tail += provider_prompt_args
            claude_tail += argv
        flags += ["--", "runuser", "-u", "node", "--", *claude_tail]
        if not tty:
            # No PTY allocated — no SIGWINCH to forward, no resize
            # bridge needed. Skip the wrapper so non-interactive
            # exec paths (e.g., provisioning shell-outs that
            # happen to go through this method) stay light.
            return flags
        return [
            sys.executable, _PTY_RESIZE_SCRIPT,
            self.name, "--", *flags,
        ]

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        """Run `claude` interactively inside the VM as the `node`
        user. Inherits the operator's terminal (stdin / stdout /
        stderr) so the session feels native. Blocks until claude
        exits; returns the in-VM exit code.

        We bypass the captured-output `machine_exec` helper here
        because that one wraps stdout/stderr in pipes — fine for
        scripted exec, wrong for an interactive shell. Drop down
        to `subprocess.run` with the TTY inherited.

        UID switches via `runuser -u node --` (not `-l`) so we
        avoid login-shell wiring. HOME / USER come from `smolvm
        -e` instead, which sets them on the process env."""
        return subprocess.run(
            self.claude_argv(argv, tty=tty), check=False,
        ).returncode

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        """Run a POSIX shell script as `user` (default `node`) and
        capture the result. Matches the docker backend's `exec`,
        which defaults to the image's USER (also node) — so test
        helpers / provision shell-outs run with the same identity
        on both backends. Pass `user="root"` for tests that need
        root.

        `runuser -u <user> -- /bin/sh -c <script>` switches UID
        without invoking a login shell; HOME / USER are set via
        `smolvm -e` (see `_env_flags_for`)."""
        argv = (
            _env_flags_for(user)
            + _guest_env_flags(self._guest_env)
            + ["--", "runuser", "-u", user, "--", "/bin/sh", "-c", script]
        )
        # _smolvm.machine_exec expects argv (the bit after `--`);
        # the -e flags go before, so call smolvm directly.
        r = subprocess.run(
            ["smolvm", "machine", "exec", "--name", self.name] + argv,
            capture_output=True, text=True, check=False,
        )
        return ExecResult(
            returncode=r.returncode,
            stdout=r.stdout or "",
            stderr=r.stderr or "",
        )

    def cp_in(self, host_path: str, container_path: str) -> None:
        """Copy a host path into the guest at `container_path`."""
        _smolvm.machine_cp(host_path, f"{self.name}:{container_path}")

    def close(self) -> None:
        # Real teardown lives on the launch ExitStack; this is just
        # the idempotent alias the BottleBackend ABC expects.
        pass
