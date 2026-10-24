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
        # Env applied at `container exec` time. The identity token is minted
        # after the container exists (its DHCP address is the registration
        # key), so it can't be in the run-time env. `exec --env` appends rather
        # than overrides, so the proxy vars are supplied only here, never at
        # run time.
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
        # Bare names: the proxy URL carries the identity token, which `ps`
        # would show.
        for name in sorted(self._exec_env):
            container_exec.extend(["--env", name])
        if tty:
            container_exec.extend(["--interactive", "--tty"])
            # Forward terminal capability hints as bare names; pty_forward
            # supplies a TERM fallback.
            for name in _terminal_env_names():
                container_exec.extend(["--env", name])
        if self.agent_workdir and self.agent_workdir != "/home/node":
            container_exec.extend(["--workdir", self.agent_workdir])
        container_exec.extend([self.name, self.agent_executable, *full_argv])
        if tty:
            # `container exec` doesn't put the terminal in raw mode, so the
            # line discipline would buffer modifier-key sequences; the wrapper
            # sets it.
            return [sys.executable, _PTY_FORWARD_SCRIPT, "--", *container_exec]
        return container_exec

    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        # `sh -lc` passes the env behind the bare `--env` names to `container
        # exec`.
        command = agent_shell_command(
            self.agent_argv(argv, tty=True), self.terminal_title, self.terminal_color)
        return command, dict(self._exec_env)

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        # Provisioning gets the agent's exec env too, so its egress carries the
        # token. Bare names again.
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
