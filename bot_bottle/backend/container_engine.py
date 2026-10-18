"""Detect a usable host container engine (Docker or Podman).

Optional everywhere it is used: the firecracker backend runs without one, but
when one is present it builds agent overlays on the host instead of inside the
infra VM, whose vfs storage can run out of space on a large base image
(PRD 0085). `BOT_BOTTLE_BUILD_ENGINE` picks one explicitly (`docker`,
`podman`) or turns host builds off (`vm`).
"""

from __future__ import annotations

import os
import shutil
import subprocess

ENGINES = ("docker", "podman")
ENGINE_ENV = "BOT_BOTTLE_BUILD_ENGINE"
_PROBE_TIMEOUT_SECONDS = 15


def _usable(engine: str) -> bool:
    """The engine is on PATH and answers `info` (a Docker daemon is reachable;
    rootless Podman needs no daemon)."""
    if shutil.which(engine) is None:
        return False
    try:
        probe = subprocess.run(
            [engine, "info"], capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def host_container_engine() -> str | None:
    """The host engine to build with: `BOT_BOTTLE_BUILD_ENGINE` if it names a
    usable one, else the first usable of Docker then Podman, else None (and
    always None when the variable is `vm`)."""
    forced = os.environ.get(ENGINE_ENV, "").strip().lower()
    if forced == "vm":
        return None
    candidates = (forced,) if forced in ENGINES else ENGINES
    for engine in candidates:
        if _usable(engine):
            return engine
    return None


__all__ = ["ENGINES", "ENGINE_ENV", "host_container_engine"]
