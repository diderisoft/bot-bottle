"""Docker availability check used by integration tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest


def docker_available() -> bool:
    if os.environ.get("SKIP_DOCKER_TESTS"):
        return False
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def skip_unless_docker(reason: str = "docker unreachable"):
    return unittest.skipUnless(docker_available(), reason)
