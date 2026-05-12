"""Docker availability check used by integration tests."""

from __future__ import annotations

import shutil
import subprocess
import unittest


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def skip_unless_docker(reason: str = "docker unreachable"):
    return unittest.skipUnless(docker_available(), reason)
