"""Host setup + status for the Docker backend.

Unlike Firecracker, the Docker backend needs no privileged one-time
host provisioning (no TAP pool / nft table) — networks and the gateway
bundle are created per-launch. So `setup()` is mostly an install/daemon
pointer, and `status()` reports whether docker is usable.

This is intentionally minimal; a richer version (daemon config checks,
gVisor/runsc install guidance, rootless-docker hints) is tracked
separately. Reached via `DockerBottleBackend.setup` / `.status`, which
the generic `./cli.py backend {setup,status}` dispatches to.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from . import util as _util


def _docker_on_path() -> bool:
    return shutil.which("docker") is not None


def _daemon_reachable() -> bool:
    if not _docker_on_path():
        return False
    return subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def _print_install_pointer() -> None:
    sys.stderr.write("Docker is required but was not found on PATH.\n")
    sys.stderr.write("  macOS: Docker Desktop  https://docs.docker.com/desktop/install/mac-install/\n")
    sys.stderr.write("  Linux: Docker Engine   https://docs.docker.com/engine/install/\n")


def setup() -> int:
    if not _docker_on_path():
        _print_install_pointer()
        return 1
    sys.stderr.write(
        "Docker backend: no privileged host setup required — networks and "
        "the gateway are created per-launch.\n"
    )
    if not _daemon_reachable():
        sys.stderr.write(
            "The Docker daemon isn't reachable; start it (e.g. Docker "
            "Desktop, or `systemctl start docker`).\n"
        )
        return 1
    if not _util.runsc_available():
        sys.stderr.write(
            "Optional: register the gVisor (`runsc`) runtime with Docker for "
            "a userspace syscall barrier; bottles auto-detect and use it.\n"
        )
    return 0


def teardown() -> int:
    sys.stderr.write(
        "Docker backend: nothing to undo — it provisions no privileged host "
        "state (networks and the gateway are per-launch and are "
        "removed by `./cli.py cleanup`). Docker itself is left installed.\n"
    )
    return 0


def status() -> int:
    ok = True
    if _docker_on_path():
        sys.stderr.write("docker on PATH: yes\n")
    else:
        sys.stderr.write("docker on PATH: NO\n")
        ok = False
    if ok and _daemon_reachable():
        sys.stderr.write("docker daemon: reachable\n")
    elif ok:
        sys.stderr.write("docker daemon: UNREACHABLE\n")
        ok = False
    runsc = _docker_on_path() and _util.runsc_available()
    sys.stderr.write(f"gVisor runsc runtime: {'registered' if runsc else 'not registered (optional)'}\n")
    if not ok:
        sys.stderr.write("\nRun: ./cli.py backend setup --backend=docker\n")
    return 0 if ok else 1
