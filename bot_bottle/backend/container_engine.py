"""Detect a usable host container engine (Docker or Podman).

Optional everywhere it is used: the firecracker backend runs without one, but
when one is present it builds agent overlays on the host, sharing its layer
cache, instead of with buildah inside the infra VM (PRD 0085). `BOT_BOTTLE_BUILD_ENGINE` picks one explicitly (`docker`,
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


class EngineUnavailable(RuntimeError):
    """`BOT_BOTTLE_BUILD_ENGINE` names an engine that can't be used, or isn't
    one of the accepted values. An explicit choice fails loudly rather than
    quietly building somewhere else."""


def host_container_engine() -> str | None:
    """The host engine to build with: the one `BOT_BOTTLE_BUILD_ENGINE` names
    (raising `EngineUnavailable` if it isn't usable), else the first usable of
    Docker then Podman, else None. `vm` forces None."""
    forced = os.environ.get(ENGINE_ENV, "").strip().lower()
    if forced == "vm":
        return None
    if forced:
        if forced not in ENGINES:
            raise EngineUnavailable(
                f"{ENGINE_ENV}={forced!r} is not one of: {', '.join((*ENGINES, 'vm'))}"
            )
        if not _usable(forced):
            raise EngineUnavailable(
                f"{ENGINE_ENV}={forced} but `{forced} info` failed; start it, or "
                f"unset {ENGINE_ENV} to fall back to another engine or the infra VM"
            )
        return forced
    for engine in ENGINES:
        if _usable(engine):
            return engine
    return None


__all__ = ["ENGINES", "ENGINE_ENV", "EngineUnavailable", "host_container_engine"]
