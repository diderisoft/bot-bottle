"""Bottle handle for a Firecracker microVM, over SSH to the guest's dropbear.

`exec` pipes a script over stdin, `cp_in` streams a tar, and `agent_command`
uses `ssh -t` (which forwards SIGWINCH). Commands run as `node` via `runuser` with
HOME/USER/PATH and the bottle env set per invocation, since the VM has no
process env of its own.
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
from ..terminal import agent_shell_command
from . import util


_HOME_FOR = {"node": "/home/node", "root": "/root"}
# The VM has no image `ENV`. A built image's is recorded with its rootfs and
# applied per command (`image_env`); a published base (a built-in harness)
# has none, so every built-in harness's `ENV PATH` entries must be here (a
# test checks the contrib Dockerfiles), or its command exits 127 on
# firecracker alone.
_DEFAULT_PATH_FOR = {
    "node": (
        "/home/node/.local/bin:"
        "/home/node/.codex/packages/standalone/current/bin:"
        "/home/node/.pi/agent/node_modules/.bin:"
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ),
    "root": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


def _env_assignments_for(user: str, env: Mapping[str, str],
                         image_env: Mapping[str, str] | None = None) -> list[str]:
    """`env` assignments for a command: HOME and USER, then the image's ENV,
    then the bottle's env over both. PATH is the bottle's, else the image's,
    else the default."""
    home = _HOME_FOR.get(user, f"/home/{user}")
    out = [f"HOME={home}", f"USER={user}"]
    image = {k: v for k, v in (image_env or {}).items()
             if k not in ("HOME", "USER") and k not in env}
    if "PATH" not in env and "PATH" not in image:
        out.append(f"PATH={_DEFAULT_PATH_FOR.get(user, _DEFAULT_PATH_FOR['root'])}")
    for k, v in [*image.items(), *env.items()]:
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
        image_env: Mapping[str, str] | None = None,
        prompt_path_in_guest: str | None = None,
        agent_command: str = "claude",
        agent_prompt_mode: PromptMode = "append_file",
        harness: str = "claude",
        terminal_title: str = "",
        terminal_color: str = "",
        agent_workdir: str = "/home/node",
    ) -> None:
        self.name = name
        self._private_key = private_key
        self._guest_ip = guest_ip
        self._guest_env = dict(guest_env or {})
        self._image_env = dict(image_env or {})
        self.prompt_path = prompt_path_in_guest
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_executable = agent_command
        self.terminal_title = terminal_title
        self.terminal_color = terminal_color
        self.harness = harness
        self.agent_workdir = agent_workdir

    def _ssh(self, *, tty: bool) -> list[str]:
        argv = util.ssh_base_argv(self._private_key, self._guest_ip)
        if tty:
            # -t: remote PTY with window-size forwarding.
            argv.insert(1, "-t")
        return argv

    def _agent_remote_argv(self, argv: list[str]) -> list[str]:
        """The `runuser ... <agent> ...` command run in the guest."""
        full_argv = list(argv)
        full_argv.extend(
            prompt_args(
                cast(PromptMode, self._agent_prompt_mode),
                self.prompt_path,
                argv=full_argv,
            )
        )
        # Run from the node workdir, not the root login's unreadable /root cwd.
        # `env --chdir` keeps the command a flat argv for quoting.
        workdir = self.agent_workdir or _HOME_FOR["node"]
        remote = ["runuser", "-u", "node", "--",
                  "env", f"--chdir={workdir}",
                  *_env_assignments_for("node", self._guest_env, self._image_env),
                  self.agent_executable, *full_argv]
        return remote

    def agent_argv(self, argv: list[str], *, tty: bool = True) -> list[str]:
        # ssh joins the remote args into one line the guest shell re-parses, so
        # quote each token.
        remote = self._agent_remote_argv(argv)
        return [*self._ssh(tty=tty), "--", *(shlex.quote(t) for t in remote)]

    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        command = agent_shell_command(
            self.agent_argv(argv, tty=True), self.terminal_title, self.terminal_color)
        return command, {}

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        # Pipe the script to `sh -s`, so nothing is quoted through ssh.
        remote = ["runuser", "-u", user, "--",
                  "env", *_env_assignments_for(user, self._guest_env, self._image_env),
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
        # Stream a tar over the exec channel: dropbear ships no scp or sftp.
        # Like `docker cp`, `container_path` becomes a copy of `host_path`.
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
        # One arg: ssh hands the guest shell a single joined string.
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
        # Teardown lives on the launch ExitStack.
        pass
