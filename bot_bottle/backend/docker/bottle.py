"""`DockerBottle`: the Bottle handle the docker backend yields."""

from __future__ import annotations

import subprocess
from typing import Callable

from typing import cast

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult
from ..terminal import agent_shell_command


class DockerBottle(Bottle):

    def __init__(
        self,
        container: str,
        teardown: Callable[[], None],
        prompt_path_in_container: str | None,
        *,
        agent_command: str = "claude",
        agent_prompt_mode: PromptMode = "append_file",
        harness: str = "claude",
        terminal_title: str = "",
        terminal_color: str = "",
        agent_workdir: str = "/home/node",
    ):
        self.name = container
        self._teardown = teardown
        self.prompt_path = prompt_path_in_container
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_executable = agent_command
        self.terminal_title = terminal_title
        self.terminal_color = terminal_color
        self.harness = harness
        self.agent_workdir = agent_workdir
        self._closed = False

    def agent_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        full_argv = list(argv)
        full_argv.extend(
            prompt_args(cast(PromptMode, self._agent_prompt_mode), self.prompt_path, argv=full_argv)
        )
        cmd = ["docker", "exec"]
        if tty:
            cmd.append("-it")
        if self.agent_workdir and self.agent_workdir != "/home/node":
            cmd.extend(["-w", self.agent_workdir])
        cmd.extend([self.name, self.agent_executable, *full_argv])
        return cmd

    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        command = agent_shell_command(
            self.agent_argv(argv, tty=True), self.terminal_title, self.terminal_color)
        return command, {}

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        # Pipe the script to `sh -s` so it never crosses argv.
        result = subprocess.run(
            ["docker", "exec", "-u", user, "-i", self.name, "sh", "-s"],
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
            ["docker", "cp", host_path, f"{self.name}:{container_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._teardown()
