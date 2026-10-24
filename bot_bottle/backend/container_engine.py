"""Detect a usable host container engine (Docker or Podman).

Optional: when present, firecracker builds agent overlays with it instead of
buildah in the infra VM. `BOT_BOTTLE_BUILD_ENGINE` picks one (`docker`,
`podman`) or disables host builds (`vm`).
"""

from __future__ import annotations

import shutil
import subprocess

ENGINES = ("docker", "podman")
ENGINE_ENV = "BOT_BOTTLE_BUILD_ENGINE"
_PROBE_TIMEOUT_SECONDS = 15


def _usable(engine: str) -> bool:
    """The engine is on PATH and answers `info`."""
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
    """`BOT_BOTTLE_BUILD_ENGINE` names an unusable or unknown engine. An
    explicit choice fails rather than building elsewhere."""


def host_container_engine(forced: str) -> str | None:
    """The engine to build with: `forced` (raising `EngineUnavailable` if
    unusable), else the first usable of Docker and Podman, else None. `vm`
    forces None."""
    forced = forced.strip().lower()
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
