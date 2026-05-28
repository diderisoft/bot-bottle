"""DockerBottle — concrete Bottle handle yielded by DockerBottleBackend."""

from __future__ import annotations

import subprocess
from typing import Callable

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult


class DockerBottle(Bottle):
    """Concrete Bottle for Docker."""

    def __init__(
        self,
        container: str,
        teardown: Callable[[], None],
        prompt_path_in_container: str | None,
        *,
        agent_command: str = "claude",
        agent_prompt_mode: PromptMode = "append_file",
    ):
        self.name = container
        self._teardown = teardown
        self._prompt_path = prompt_path_in_container
        self._agent_prompt_mode = agent_prompt_mode
        self.agent_command = agent_command
        self.agent_provider_template = (
            "codex" if agent_command == "codex" else "claude"
        )
        self._closed = False

    def agent_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        full_argv = list(argv)
        full_argv.extend(
            prompt_args(self._agent_prompt_mode, self._prompt_path, argv=full_argv)
        )
        cmd = ["docker", "exec"]
        if tty:
            cmd.append("-it")
        cmd.extend([self.name, self.agent_command, *full_argv])
        return cmd

    def exec_agent(self, argv: list[str], *, tty: bool = True) -> int:
        return subprocess.run(
            self.agent_argv(argv, tty=tty), check=False,
        ).returncode

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        # Pipe via stdin to `sh -s` so the caller never has to worry
        # about quoting; the script source lands inside the container
        # without crossing argv. `-u <user>` overrides the image's
        # default USER — defaults to `node` which is already the
        # image's USER, so the explicit flag is a no-op there but
        # keeps the cross-backend contract uniform.
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
