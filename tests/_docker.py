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


def skip_unless_docker_or_firecracker(
    reason: str = "neither Docker nor Firecracker selected",
):
    """Skip a backend-agnostic test unless one supported backend can run.

    Firecracker does not require the host Docker daemon. The KVM coverage job
    deliberately sets ``SKIP_DOCKER_TESTS`` to exclude Docker-only integration
    classes while still exercising this path.
    """
    firecracker_selected = os.environ.get("BOT_BOTTLE_BACKEND") == "firecracker"
    return unittest.skipUnless(firecracker_selected or docker_available(), reason)
