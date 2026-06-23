"""Bottle handle for Apple's `container` CLI."""

from __future__ import annotations

import os
import subprocess
from typing import Callable, cast

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult
from ..terminal import exec_shell_script


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
    ):
        self.name = container
        self._teardown = teardown
        self.prompt_path = prompt_path_in_container
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_command = agent_command
        self.terminal_title = terminal_title
        self.terminal_color = terminal_color
        self.agent_provider_template = agent_provider_template
        self.agent_workdir = agent_workdir
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
        cmd = ["container", "exec"]
        if tty:
            cmd.extend(["--interactive", "--tty"])
            # Forward TERM so Claude Code can enable modifier-key protocols
            # (e.g. modifyOtherKeys). Without it the inner PTY session has no
            # TERM and Shift+Enter is indistinguishable from plain Enter.
            term = os.environ.get("TERM", "xterm-256color")
            cmd.extend(["--env", f"TERM={term}"])
        if self.agent_workdir and self.agent_workdir != "/home/node":
            cmd.extend(["--workdir", self.agent_workdir])
        cmd.extend([self.name, self.agent_command, *full_argv])
        return cmd

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
        result = subprocess.run(
            ["container", "exec", "--user", user, "--interactive",
             self.name, "sh", "-s"],
            input=script,
            capture_output=True,
            text=True,
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
