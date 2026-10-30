"""`DockerBottle`: the Bottle handle the docker backend yields."""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from typing import cast

from ...agent_provider import PromptMode, prompt_args
from .. import Bottle, ExecResult
from ..base import run_script
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
        exec_env: dict[str, str] | None = None,
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
        # Env applied at `docker exec` time, never in the container's own: the
        # proxy URL carries this run's identity token, so a container kept for
        # the next run (`preserve_container`) mustn't hold it.
        self._exec_env = dict(exec_env or {})
        self._closed = False

    def _env_flags(self) -> list[str]:
        # Bare names: the values come from the process env, off `ps`.
        return [flag for name in sorted(self._exec_env) for flag in ("-e", name)]

    def _process_env(self) -> dict[str, str] | None:
        return {**os.environ, **self._exec_env} if self._exec_env else None

    def agent_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        full_argv = list(argv)
        full_argv.extend(
            prompt_args(cast(PromptMode, self._agent_prompt_mode), self.prompt_path, argv=full_argv)
        )
        cmd = ["docker", "exec", *self._env_flags()]
        if tty:
            cmd.append("-it")
        if self.agent_workdir and self.agent_workdir != "/home/node":
            cmd.extend(["-w", self.agent_workdir])
        cmd.extend([self.name, self.agent_executable, *full_argv])
        return cmd

    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        command = agent_shell_command(
            self.agent_argv(argv, tty=True), self.terminal_title, self.terminal_color)
        return command, dict(self._exec_env)

    def exec(self, script: str, *, user: str = "node", timeout: float | None = None,
             private: bool = True) -> ExecResult:
        del private  # stdin always: `docker exec -i` doesn't leak
        # Pipe the script to `sh -s` so it never crosses argv.
        return run_script(["docker", "exec", *self._env_flags(), "-u", user, "-i", self.name,
                           "sh", "-s"], script, timeout=timeout, env=self._process_env())

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
