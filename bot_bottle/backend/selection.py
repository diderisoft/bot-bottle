"""Backend registry, selection and cross-backend enumeration. Backends are
imported lazily.
"""

from __future__ import annotations

import sys
from typing import Any

from ..contract.hostctl import Settings
from ..lib.log import die, info, warn
from ..util import read_tty_line
from .base import ActiveAgent, BackendStatus, BottleBackend

BACKEND_NAMES = ("docker", "firecracker", "macos-container")


def _get_backends(settings: Settings) -> dict[str, BottleBackend[Any, Any]]:
    """Every backend, constructed with `settings`."""
    from .docker import DockerBottleBackend
    from .firecracker import FirecrackerBottleBackend
    from .macos_container import MacosContainerBottleBackend
    return {
        "docker": DockerBottleBackend(settings),
        "firecracker": FirecrackerBottleBackend(settings),
        "macos-container": MacosContainerBottleBackend(settings),
    }


def get_bottle_backend(
    settings: Settings,
    name: str | None = None,
    *,
    prompt: bool = True,
) -> BottleBackend[Any, Any]:
    """Resolve the backend: `name`, else `settings.operation.backend`, else
    auto-selection. With `prompt=False` the docker fallback dies instead of
    prompting. Dies on an unknown name."""
    resolved = name or settings.operation.backend or None
    if resolved is None:
        resolved = _auto_select_backend(settings, prompt=prompt)
    backends = _get_backends(settings)
    if resolved not in backends:
        known = ", ".join(sorted(backends))
        die(f"unknown backend {resolved!r}; known backends: {known}")
    return backends[resolved]


def _platform_vm_suggestion() -> str:
    """VM backend to suggest on this platform."""
    return "macos-container" if sys.platform == "darwin" else "firecracker"


def _print_vm_install_instructions() -> None:
    """Print VM backend install instructions to stderr."""
    vm = _platform_vm_suggestion()
    if vm == "macos-container":
        info("Install Apple Container: https://github.com/apple/container/releases")
        info("Then start the service:  container system start")
    else:
        info("Install Firecracker: https://github.com/firecracker-microvm/firecracker/releases")
        info("Configure the host:  bot-bottle backend setup")


def _auto_select_backend(settings: Settings, prompt: bool = True) -> str:
    """macos-container if installed, else firecracker on a KVM-capable host
    (even before the binary is installed, so its preflight explains), else
    docker after a warning and a prompt.
    """
    if has_backend(settings, "macos-container"):
        return "macos-container"
    from .firecracker import FirecrackerBottleBackend
    if FirecrackerBottleBackend.is_host_capable():
        return "firecracker"

    if not has_backend(settings, "docker"):
        info("No backend available on this host.")
        _print_vm_install_instructions()
        die("no backend available; install a VM backend and re-run")

    vm = _platform_vm_suggestion()
    warn(
        "docker is less secure than VM backends — "
        "containers share the host kernel."
    )
    if not prompt:
        die(
            f"no VM backend available; set BOT_BOTTLE_BACKEND=docker to proceed "
            f"with docker, or install the {vm!r} backend."
        )
    sys.stderr.write(
        f"bot-bottle: For better isolation, install the {vm!r} backend.\n"
        f"  [i] show {vm} install instructions and exit\n"
        "  [d] use docker anyway\n"
        "  [q] quit\n"
        "bot-bottle: choice [i/d/q]: "
    )
    sys.stderr.flush()
    reply = read_tty_line().strip().lower()
    if reply == "d":
        return "docker"
    if reply == "i":
        _print_vm_install_instructions()
    die("not proceeding with docker; install a VM backend or set BOT_BOTTLE_BACKEND=docker")


def known_backend_names() -> tuple[str, ...]:
    """Sorted backend names."""
    return BACKEND_NAMES


def has_backend(settings: Settings, name: str) -> bool:
    """Whether the named backend's prerequisites are met here. False for
    unknown names."""
    backends = _get_backends(settings)
    if name not in backends:
        return False
    return backends[name].is_available()


def is_backend_available(settings: Settings, name: str) -> bool:
    """Cheap check: is the backend's binary on PATH? Doesn't probe daemons or
    the network pool."""
    return has_backend(settings, name)


def is_backend_ready(settings: Settings, name: str, *, quiet: bool = False) -> bool:
    """Full readiness: all of the backend's `status()` checks pass. Prints
    what's missing unless `quiet`. False for unknown names."""
    backends = _get_backends(settings)
    if name not in backends:
        return False
    return backends[name].status(quiet=quiet) == BackendStatus.READY


def enumerate_active_agents(settings: Settings) -> list[ActiveAgent]:
    """Running agents across every available backend, sorted by `(started_at,
    slug)` so the order is stable."""
    out: list[ActiveAgent] = []
    backends = _get_backends(settings)
    for name in sorted(backends):
        if not backends[name].is_available():
            continue
        out.extend(backends[name].enumerate_active())
    out.sort(key=lambda a: (a.started_at, a.slug))
    return out
