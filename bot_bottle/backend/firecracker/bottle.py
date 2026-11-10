"""Bottle handle for a Firecracker microVM, over SSH to the guest's dropbear.

`exec` pipes a script over stdin, `cp_in` streams a tar, and `agent_command`
uses `ssh -t` (which forwards SIGWINCH). dropbear refuses an exec request
longer than its `MAX_CMD_LEN` (9000 bytes), so `agent_command` stages any long
argument (a task prompt) as a file in the guest and the guest shell reads it
back. Commands run as `node` via `runuser` with
HOME/USER/PATH and the bottle env set per invocation, since the VM has no
process env of its own. No env value goes on the ssh command line, which any
host process can read: `exec` exports them at the top of the script it pipes,
and `agent_argv` writes them to a guest file (`_GUEST_ENV_FILE`) first.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, cast

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult
from ..base import run_script
from ..terminal import agent_shell_command
from . import util


_HOME_FOR = {"node": "/home/node", "root": "/root"}
# Arguments longer than this go to a guest file, so the agent's command stays
# far under dropbear's MAX_CMD_LEN (9000 bytes) however long the prompt is.
LONG_ARG_BYTES = 1024
# Where they go: root's (the ssh user's) home, unreadable to the agent.
_GUEST_ARGS_DIR = "/root/.bot-bottle-args"
# The agent's env, rewritten before each agent command: on the guest's tmpfs
# `/run`, readable by `node` (the agent), writable only by root.
_GUEST_ENV_DIR = "/run/bot-bottle-agent"
_GUEST_ENV_FILE = f"{_GUEST_ENV_DIR}/agent.env"
# Run the agent from its workdir with the env file's assignments: the file
# prepends them to "$@", so this is the `env --chdir=<workdir> K=v ... <agent>`
# it always was, with the values off the command line.
_AGENT_SHELL = f'wd=$1; shift; . {_GUEST_ENV_FILE} && exec env --chdir="$wd" "$@"'
# Run by the ssh user (root) with the file on stdin; renamed into place, so a
# concurrent command never reads half of one.
_WRITE_ENV_FILE = (
    f'g=$(id -g node) && umask 027 && mkdir -p {_GUEST_ENV_DIR} && '
    f'chgrp "$g" {_GUEST_ENV_DIR} && chmod 750 {_GUEST_ENV_DIR} && '
    f't={_GUEST_ENV_FILE}.$$ && cat > "$t" && chgrp "$g" "$t" && mv -f "$t" {_GUEST_ENV_FILE}'
)
_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The VM has no image `ENV`. A built image's is recorded with its rootfs and
# applied per command (`image_env`); a published base (a built-in harness)
# has none, so every built-in harness's `ENV PATH` entries must be here (a
# test checks the contrib Dockerfiles), or its command exits 127 on
# firecracker alone. The image's own directories come before the home's: a
# home is kept across stop and resume while the rootfs is fresh, so nothing
# left there (a harness's self-update, say) may shadow the image's (#854).
_DEFAULT_PATH_FOR = {
    "node": (
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:"
        "/home/node/.local/bin:"
        "/home/node/.codex/packages/standalone/current/bin:"
        "/home/node/.pi/agent/node_modules/.bin"
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


def _env_exports(assignments: list[str]) -> str:
    """`export` lines setting `assignments`, in order, for a shell script.
    A name no shell variable can have is left out."""
    lines = []
    for assignment in assignments:
        name, _, value = assignment.partition("=")
        if _SHELL_NAME.fullmatch(name):
            lines.append(f"export {name}={shlex.quote(value)}\n")
    return "".join(lines)


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
        # Run from the node workdir, not the root login's unreadable /root cwd,
        # with the env `_write_agent_env` put in the guest.
        workdir = self.agent_workdir or _HOME_FOR["node"]
        remote = ["runuser", "-u", "node", "--",
                  "/bin/sh", "-c", _AGENT_SHELL, "sh", workdir,
                  self.agent_executable, *full_argv]
        return remote

    def _agent_env_file(self) -> str:
        """The guest env file's contents: a `set --` that puts the agent's
        `env` assignments before the command in "$@"."""
        assignments = _env_assignments_for("node", self._guest_env, self._image_env)
        return f"set -- {shlex.join(assignments)} \"$@\"\n"

    def _write_agent_env(self) -> None:
        """Write the agent's env to `_GUEST_ENV_FILE` over ssh's stdin."""
        result = run_script([*self._ssh(tty=False), "--", _WRITE_ENV_FILE],
                            self._agent_env_file())
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            raise subprocess.CalledProcessError(
                result.returncode, ["write agent env", _GUEST_ENV_FILE])

    def agent_argv(self, argv: list[str], *, tty: bool = True,
                   stage_long: bool = False) -> list[str]:
        # ssh joins the remote args into one line the guest shell re-parses, so
        # quote each token. With `stage_long`, a long token is written to a
        # guest file and read back by the guest shell as one argument.
        self._write_agent_env()
        tokens: list[str] = []
        for i, token in enumerate(self._agent_remote_argv(argv)):
            if stage_long and len(token.encode()) > LONG_ARG_BYTES:
                path = self._stage_arg(f"arg{i}", token)
                tokens.append(f'"$(cat {shlex.quote(path)})"')
            else:
                tokens.append(shlex.quote(token))
        return [*self._ssh(tty=tty), "--", *tokens]

    def _stage_arg(self, name: str, value: str) -> str:
        """Write `value` to a guest file only root can read; its path."""
        path = f"{_GUEST_ARGS_DIR}/{name}"
        with tempfile.TemporaryDirectory(prefix="bot-bottle-arg.") as tmp:
            host = Path(tmp) / name
            # `$(cat …)` drops trailing newlines; nothing else changes.
            host.write_text(value, encoding="utf-8")
            host.chmod(0o600)
            self.cp_in(str(host), path)
        return path

    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        command = agent_shell_command(
            self.agent_argv(argv, tty=True, stage_long=True),
            self.terminal_title, self.terminal_color)
        return command, {}

    def exec(self, script: str, *, user: str = "node", timeout: float | None = None,
             private: bool = True) -> ExecResult:
        del private  # stdin always: ssh doesn't leak
        # Pipe the script to `sh -s`, so nothing is quoted through ssh, after
        # the env's exports, so no value is on the command line either.
        remote = ["runuser", "-u", user, "--", "/bin/sh", "-s"]
        exports = _env_exports(_env_assignments_for(user, self._guest_env, self._image_env))
        return run_script([*self._ssh(tty=False), "--", *remote], exports + script,
                          timeout=timeout)

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
