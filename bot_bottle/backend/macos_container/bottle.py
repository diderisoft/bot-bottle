"""Bottle handle for Apple's `container` CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable, cast

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult
from ..terminal import agent_shell_command
from . import pty_forward as _pty_forward


_PTY_FORWARD_SCRIPT = _pty_forward.__file__
_TERMINAL_ENV_NAMES = (
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "KITTY_WINDOW_ID",
    "KITTY_PID",
    "WEZTERM_PANE",
    "WEZTERM_UNIX_SOCKET",
    "GHOSTTY_BIN_DIR",
    "GHOSTTY_RESOURCES_DIR",
    "ITERM_SESSION_ID",
    "VTE_VERSION",
    "KONSOLE_VERSION",
    "ALACRITTY_WINDOW_ID",
)


def _terminal_env_names() -> tuple[str, ...]:
    return tuple(
        name for name in _TERMINAL_ENV_NAMES
        if name == "TERM" or os.environ.get(name)
    )


class MacosContainerBottle(Bottle):
    def __init__(
        self,
        container: str,
        teardown: Callable[[], None],
        prompt_path_in_container: str | None,
        *,
        agent_command: str = "claude",
        agent_prompt_mode: PromptMode = "append_file",
        agent_provider_template: str = "claude",
        terminal_title: str = "",
        terminal_color: str = "",
        agent_workdir: str = "/home/node",
        exec_env: dict[str, str] | None = None,
    ):
        self.name = container
        self._teardown = teardown
        self.prompt_path = prompt_path_in_container
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_executable = agent_command
        self.terminal_title = terminal_title
        self.terminal_color = terminal_color
        self.agent_provider_template = agent_provider_template
        self.agent_workdir = agent_workdir
        # Env applied to the agent process at `container exec` time, on top of
        # what the container was run with. This is how the identity token
        # reaches the agent: registration mints it *after* the
        # container exists — its source IP is the registration key and Apple
        # Container assigns that by DHCP — so it cannot be in the run-time env
        # the way docker's compose spec does it.
        #
        # `container exec --env` does NOT override a run-time value — it
        # appends, leaving duplicate entries in the agent's `environ` whose
        # resolution is runtime-specific (Node last-wins, Rust first-wins). So
        # nothing here may rely on superseding: the proxy vars are supplied
        # *only* at exec time and are deliberately absent from the run-time
        # env. See `launch._agent_env_entries`.
        self._exec_env = dict(exec_env or {})
        self._closed = False

    def agent_argv(self, argv: list[str], *, tty: bool = True) -> list[str]:
        full_argv = list(argv)
        full_argv.extend(
            prompt_args(
                cast(PromptMode, self._agent_prompt_mode),
                self.prompt_path,
                argv=full_argv,
            )
        )
        container_exec = ["container", "exec"]
        # Bare env names, same rule as the terminal hints below: the value
        # stays in the child env `exec_agent` builds and never reaches argv —
        # the proxy URL here carries the identity token, which `ps` would
        # otherwise expose to every process on the host.
        for name in sorted(self._exec_env):
            container_exec.extend(["--env", name])
        if tty:
            container_exec.extend(["--interactive", "--tty"])
            # Forward terminal capability hints so TUIs can enable modified-key
            # protocols. Use bare env names: values stay in the child env, not
            # on argv, and pty_forward supplies a TERM fallback when needed.
            for name in _terminal_env_names():
                container_exec.extend(["--env", name])
        if self.agent_workdir and self.agent_workdir != "/home/node":
            container_exec.extend(["--workdir", self.agent_workdir])
        container_exec.extend([self.name, self.agent_executable, *full_argv])
        if tty:
            # Wrap with the raw-mode forwarder: container exec does not put
            # the host terminal into raw mode itself, so the line discipline
            # buffers modifier-key sequences until CR.  The wrapper sets raw
            # mode before exec and restores it on exit.
            return [sys.executable, _PTY_FORWARD_SCRIPT, "--", *container_exec]
        return container_exec

    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        # The env carries the values behind the bare `--env` names in
        # `agent_argv`; `sh -lc` passes it through to `container exec`.
        command = agent_shell_command(
            self.agent_argv(argv, tty=True), self.terminal_title, self.terminal_color)
        return command, dict(self._exec_env)

    def exec_agent(self, argv: list[str], *, tty: bool = True) -> int:
        if tty:
            command, extra = self.agent_command(argv)
        else:
            command, extra = self.agent_argv(argv, tty=False), dict(self._exec_env)
        env = {**os.environ, **extra} if extra else None
        return subprocess.run(command, env=env, check=False).returncode

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        # Carry the same exec env the agent gets: provisioning steps run
        # through here, and a provider whose provision step fetches anything
        # would egress without the identity token and be denied by /resolve.
        # Bare `--env NAME` again, so the token stays off argv.
        argv = ["container", "exec", "--user", user, "--interactive"]
        for name in sorted(self._exec_env):
            argv.extend(["--env", name])
        argv.extend([self.name, "sh", "-s"])
        result = subprocess.run(
            argv,
            input=script,
            capture_output=True,
            text=True,
            env={**os.environ, **self._exec_env} if self._exec_env else None,
            check=False,
        )
        return ExecResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def cp_in(self, host_path: str, container_path: str) -> None:
        subprocess.run(
            ["container", "cp", host_path, f"{self.name}:{container_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._teardown()
