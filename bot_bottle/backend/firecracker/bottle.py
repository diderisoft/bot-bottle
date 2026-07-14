"""Bottle handle for a Firecracker microVM, over SSH.

Every operation routes through SSH to the guest (dropbear, listening on
the TAP link). `exec` pipes a script over stdin and captures output;
`cp_in` uses scp; `exec_agent` uses `ssh -t` for an interactive PTY
session. `ssh -t` forwards the host terminal's SIGWINCH to the remote
PTY natively, so no separate resize bridge is needed.

Commands run as the image's `node` user via `runuser`, with HOME/USER/
PATH and the bottle env (HTTPS_PROXY at the gateway, CA paths, …) set
per-invocation through `env` (the VM itself just runs the init; it has
no baked-in process env like a `docker run` container would).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping, cast

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult
from ..terminal import exec_shell_script
from . import util


_HOME_FOR = {"node": "/home/node", "root": "/root"}
_DEFAULT_PATH_FOR = {
    "node": (
        "/home/node/.local/bin:"
        "/home/node/.codex/packages/standalone/current/bin:"
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ),
    "root": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


def _env_assignments_for(user: str, env: Mapping[str, str]) -> list[str]:
    home = _HOME_FOR.get(user, f"/home/{user}")
    out = [f"HOME={home}", f"USER={user}"]
    if "PATH" not in env:
        out.append(f"PATH={_DEFAULT_PATH_FOR.get(user, _DEFAULT_PATH_FOR['root'])}")
    for k, v in env.items():
        out.append(f"{k}={v}")
    return out


class FirecrackerBottle(Bottle):
    def __init__(
        self,
        name: str,
        *,
        private_key: Path,
        guest_ip: str,
        guest_env: Mapping[str, str] | None = None,
        prompt_path_in_guest: str | None = None,
        agent_command: str = "claude",
        agent_prompt_mode: PromptMode = "append_file",
        agent_provider_template: str = "claude",
        terminal_title: str = "",
        terminal_color: str = "",
        agent_workdir: str = "/home/node",
    ) -> None:
        self.name = name
        self._private_key = private_key
        self._guest_ip = guest_ip
        self._guest_env = dict(guest_env or {})
        self.prompt_path = prompt_path_in_guest
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_command = agent_command
        self.terminal_title = terminal_title
        self.terminal_color = terminal_color
        self.agent_provider_template = agent_provider_template
        self.agent_workdir = agent_workdir

    def _ssh(self, *, tty: bool) -> list[str]:
        argv = util.ssh_base_argv(self._private_key, self._guest_ip)
        if tty:
            # -t: allocate a remote PTY and forward window-size changes.
            argv.insert(1, "-t")
        return argv

    def _agent_remote_argv(self, argv: list[str]) -> list[str]:
        """The `runuser … <agent> …` command run inside the guest."""
        full_argv = list(argv)
        full_argv.extend(
            prompt_args(
                cast(PromptMode, self._agent_prompt_mode),
                self.prompt_path,
                argv=full_argv,
            )
        )
        # ALWAYS cd into the workdir before exec'ing the agent. The
        # control SSH logs in as root, so the inherited cwd is /root —
        # which the agent (running as node) can't even read now that
        # /root is root-owned. Run the agent from the node workdir
        # (default /home/node) so its cwd is node-accessible; otherwise
        # Node's process.cwd(), the shell-snapshot machinery, and
        # `/doctor` all fail on the unreadable /root.
        # Run the agent from the node workdir (default /home/node), NOT
        # the /root cwd inherited from the root SSH login — /root is now
        # root-owned and unreadable by node, which breaks Node's
        # process.cwd(), the shell-snapshot machinery, and `/doctor`.
        # Use `env --chdir` rather than a `sh -c 'cd … && exec "$@"'`
        # wrapper: ssh space-joins everything after the host into one
        # string for the guest shell, so a quoted script + $@ would be
        # re-split and mangled (exec'ing the $0 placeholder). All-simple
        # words survive that join.
        workdir = self.agent_workdir or _HOME_FOR["node"]
        remote = ["runuser", "-u", "node", "--",
                  "env", f"--chdir={workdir}",
                  *_env_assignments_for("node", self._guest_env),
                  self.agent_command, *full_argv]
        return remote

    def agent_argv(self, argv: list[str], *, tty: bool = True) -> list[str]:
        return [*self._ssh(tty=tty), "--", *self._agent_remote_argv(argv)]

    def exec_agent(self, argv: list[str], *, tty: bool = True) -> int:
        agent_argv = self.agent_argv(argv, tty=tty)
        script = (
            exec_shell_script(agent_argv, self.terminal_title, self.terminal_color)
            if tty else None
        )
        if script is None:
            return subprocess.run(agent_argv, check=False).returncode
        return subprocess.run(["sh", "-lc", script], check=False).returncode

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        # Pipe the script over stdin (`sh -s`) so nothing needs shell
        # quoting through the SSH command line.
        remote = ["runuser", "-u", user, "--",
                  "env", *_env_assignments_for(user, self._guest_env),
                  "/bin/sh", "-s"]
        result = subprocess.run(
            [*self._ssh(tty=False), "--", *remote],
            input=script, capture_output=True, text=True, check=False,
        )
        return ExecResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def cp_in(self, host_path: str, container_path: str) -> None:
        # Stream a tar over the SSH exec channel rather than scp: the
        # guest runs dropbear, which ships no sftp-server/scp, but every
        # agent image has `tar`. Copy so `container_path` becomes a copy
        # of `host_path` (docker cp semantics — callers rm -rf the
        # destination first).
        host_parent = os.path.dirname(host_path.rstrip("/")) or "/"
        host_base = os.path.basename(host_path.rstrip("/"))
        guest_parent = os.path.dirname(container_path.rstrip("/")) or "/"
        guest_base = os.path.basename(container_path.rstrip("/"))
        remote = (
            f"mkdir -p {shlex.quote(guest_parent)} && "
            f"cd {shlex.quote(guest_parent)} && "
            f"rm -rf {shlex.quote(guest_base)} && tar -xf - && "
            f"{{ [ {shlex.quote(host_base)} = {shlex.quote(guest_base)} ] || "
            f"mv {shlex.quote(host_base)} {shlex.quote(guest_base)}; }}"
        )
        tar = subprocess.Popen(
            ["tar", "-C", host_parent, "-cf", "-", host_base],
            stdout=subprocess.PIPE,
        )
        # Pass the command as one arg: ssh space-joins everything after
        # the host into a single string for the guest's login shell, so a
        # `sh -c <remote>` split would drop everything past the first word
        # (the guest shell runs `<remote>` directly; stdin carries the
        # tar).
        ssh = subprocess.run(
            [*self._ssh(tty=False), "--", remote],
            stdin=tar.stdout, capture_output=True, text=True, check=False,
        )
        tar.wait()
        if tar.returncode != 0 or ssh.returncode != 0:
            sys.stderr.write(ssh.stderr)
            raise subprocess.CalledProcessError(
                ssh.returncode or tar.returncode,
                ["cp_in", host_path, container_path],
            )

    def close(self) -> None:
        # Real teardown (VM terminate, TAP release) lives on the launch
        # ExitStack; this is the idempotent alias the ABC expects.
        pass
