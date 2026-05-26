"""DockerBottle — concrete Bottle handle yielded by
DockerBottleBackend.launch.

Holds the container name plus the in-container prompt path so
exec_claude can transparently add --append-system-prompt-file when a
prompt was provisioned.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from .. import Bottle, ExecResult


class DockerBottle(Bottle):
    """Concrete Bottle for Docker."""

    def __init__(
        self,
        container: str,
        teardown: Callable[[], None],
        prompt_path_in_container: str | None,
    ):
        self.name = container
        self._teardown = teardown
        self._prompt_path = prompt_path_in_container
        self._closed = False

    def claude_docker_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        """Return the full `docker exec` argv for running claude in
        this bottle. Public so callers that want to spawn claude
        somewhere other than the dashboard's foreground (e.g.,
        `tmux split-window` / `tmux respawn-pane` from the dashboard
        when `$TMUX` is set) can build on the same command without
        duplicating the `--append-system-prompt-file` plumbing."""
        full_argv = list(argv)
        if self._prompt_path:
            full_argv.extend(["--append-system-prompt-file", self._prompt_path])
        cmd = ["docker", "exec"]
        if tty:
            cmd.append("-it")
        cmd.extend([self.name, "claude", *full_argv])
        return cmd

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        return subprocess.run(
            self.claude_docker_argv(argv, tty=tty), check=False,
        ).returncode

    def exec(self, script: str) -> ExecResult:
        # Pipe via stdin to `sh -s` so the caller never has to worry
        # about quoting; the script source lands inside the container
        # without crossing argv.
        result = subprocess.run(
            ["docker", "exec", "-i", self.name, "sh", "-s"],
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
