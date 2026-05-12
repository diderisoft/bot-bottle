"""DockerBottle — concrete Bottle handle yielded by
DockerBottleBackend.launch.

Holds the container name plus the in-container prompt path so
exec_claude can transparently add --append-system-prompt-file when a
prompt was provisioned.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from .. import Bottle


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

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        full_argv = list(argv)
        if self._prompt_path:
            full_argv.extend(["--append-system-prompt-file", self._prompt_path])
        cmd = ["docker", "exec"]
        if tty:
            cmd.append("-it")
        cmd.extend([self.name, "claude", *full_argv])
        return subprocess.run(cmd).returncode

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
