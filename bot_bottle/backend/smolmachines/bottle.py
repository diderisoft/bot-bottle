"""SmolmachinesBottle — running-instance handle (PRD 0023 chunk 2d).

Routes `exec_agent` / `exec` / `cp_in` through `smolvm machine
exec` / `smolvm machine cp`. The handle is yielded by `launch`
and torn down via the surrounding ExitStack on context exit;
`close` is a no-op idempotent alias so the BottleBackend ABC's
context-manager contract is satisfied.

User context: `smolvm machine exec` runs commands as root in the
VM, but the agent image's USER is `node` and agent CLIs may refuse
to run as root in bypass modes. Both
`exec_agent` and `exec` switch to the requested user (default
`node`) via `runuser -u <user> --` and set `HOME` / `USER`
through `smolvm -e` — avoiding `runuser -l`'s login-shell wiring
(PAM session setup, /etc/profile sourcing) which can hang on a
minimal Debian VM with no PAM session config."""

from __future__ import annotations

import subprocess
import sys
import time
import shlex
from typing import Mapping, cast

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult
from ..terminal import exec_shell_script
from . import pty_resize as _pty_resize
from . import smolvm as _smolvm


# Absolute path to the pty_resize wrapper. Invoke as
# `python <path>` rather than `python -m <dotted-path>` so the
# wrapper runs regardless of cwd / sys.path — it has no
# bot_bottle.* imports, so it's self-contained.
_PTY_RESIZE_SCRIPT = _pty_resize.__file__


# Per-user env the agent image's USER (node) expects. Some providers
# write session state under the user's home directory;
# bare `runuser -u` inherits root's HOME=/root, which claude
# can't write to. Set HOME / USER explicitly through smolvm -e
# so the child process sees them.
_HOME_FOR = {
    "node": "/home/node",
    "root": "/root",
}


def _env_assignments_for(user: str, env: Mapping[str, str]) -> list[str]:
    home = _HOME_FOR.get(user, f"/home/{user}")
    out = [f"HOME={home}", f"USER={user}"]
    for k, v in env.items():
        out.append(f"{k}={v}")
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
        agent_prompt_mode: PromptMode = "append_file",
        agent_provider_template: str = "claude",
        terminal_title: str = "",
        terminal_color: str = "",
        agent_workdir: str = "/home/node",
    ) -> None:
        self.name = machine_name
        # In-VM path to the agent's prompt file. None when the
        # agent declared no prompt (file still exists; we just
        # don't pass --append-system-prompt-file).
        self.prompt_path = prompt_path
        # Env vars the agent process needs (HTTPS_PROXY,
        # CLAUDE_CODE_OAUTH_TOKEN, manifest-declared bottle env, …).
        # Forwarded on every `smolvm machine exec` via `-e K=V`
        # because exec doesn't inherit from machine_create's env.
        self._guest_env = dict(guest_env or {})
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_command = agent_command
        self.terminal_title = terminal_title
        self.terminal_color = terminal_color
        self.agent_provider_template = agent_provider_template
        self.agent_workdir = agent_workdir

    def agent_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        flags = ["smolvm", "machine", "exec", "--name", self.name]
        if tty:
            flags += ["-i", "-t"]
        agent_tail = ["env", *_env_assignments_for("node", self._guest_env)]
        if self.agent_workdir and self.agent_workdir != _HOME_FOR["node"]:
            agent_tail += [
                "sh", "-lc",
                f"cd {shlex.quote(self.agent_workdir)} && exec \"$@\"",
                "bot-bottle-agent",
            ]
        agent_tail.append(self.agent_command)
        provider_prompt_args = prompt_args(
            cast(PromptMode, self._agent_prompt_mode), self.prompt_path, argv=argv,
        )
        if cast(PromptMode, self._agent_prompt_mode) == "read_prompt_file":
            agent_tail += argv
            agent_tail += provider_prompt_args
        else:
            agent_tail += provider_prompt_args
            agent_tail += argv
        flags += ["--", "runuser", "-u", "node", "--", *agent_tail]
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

    def exec_agent(self, argv: list[str], *, tty: bool = True) -> int:
        """Run the selected agent interactively inside the VM as the `node`
        user. Inherits the operator's terminal (stdin / stdout /
        stderr) so the session feels native. Blocks until the agent
        exits; returns the in-VM exit code.

        We bypass the captured-output `machine_exec` helper here
        because that one wraps stdout/stderr in pipes — fine for
        scripted exec, wrong for an interactive shell. Drop down
        to `subprocess.run` with the TTY inherited.

        UID switches via `runuser -u node --` (not `-l`) so we
        avoid login-shell wiring. HOME / USER come from `smolvm
        -e` instead, which sets them on the process env."""
        agent_argv = self.agent_argv(argv, tty=tty)
        script = exec_shell_script(agent_argv, self.terminal_title, self.terminal_color) if tty else None
        if script is None:
            return subprocess.run(agent_argv, check=False).returncode
        return subprocess.run(["sh", "-lc", script], check=False).returncode

    # smolvm/libkrun can SIGKILL an otherwise-normal exec during
    # early-VM provisioning. Retry once after a short settle so
    # callers (provision_ca, etc.) don't have to handle it themselves.
    _SIGKILL_EXIT = 128 + 9

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        """Run a POSIX shell script as `user` (default `node`) and
        capture the result. Matches the docker backend's `exec`,
        which defaults to the image's USER (also node) — so test
        helpers / provision shell-outs run with the same identity
        on both backends. Pass `user="root"` for tests that need
        root.

        `runuser -u <user> -- env ... /bin/sh -c <script>` switches UID
        without invoking a login shell, then sets HOME / USER and the
        bottle env in the child process.

        Retries once on SIGKILL (exit 137) — libkrun occasionally
        kills short-lived execs during VM bring-up."""
        r = self._exec_raw(script, user=user)
        if r.returncode == self._SIGKILL_EXIT:
            time.sleep(1.0)
            r = self._exec_raw(script, user=user)
        return r

    def _exec_raw(self, script: str, *, user: str = "node") -> ExecResult:
        argv = [
            "--", "runuser", "-u", user, "--",
            "env", *_env_assignments_for(user, self._guest_env),
            "/bin/sh", "-c", script,
        ]
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
