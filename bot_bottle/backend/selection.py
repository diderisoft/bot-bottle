"""Backend registry, selection, and active-agent enumeration.

Resolves which bottle backend to use (explicit name / `settings.operation.backend`
/ auto-select), and enumerates running agents across every available backend.
The three concrete backends are imported lazily inside `_get_backends` so this
module — and anything that only needs to *select* a backend — stays cheap.
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
    """Every backend, constructed with `settings`. The concrete classes are
    imported here so importing one backend's modules doesn't load the others."""
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
    """Resolve the bottle backend.

    `name` precedence:
      1. explicit arg (e.g. resume passes the recorded backend name)
      2. `settings.operation.backend` (BOT_BOTTLE_BACKEND)
      3. auto-selection: VM backend first, docker fallback with prompt

    `prompt` controls whether auto-selection may block on an interactive
    [i/d/q] prompt when falling back to docker. Pass `prompt=False` in
    non-interactive contexts (headless launches, CI) so the call dies
    with an actionable message instead of hanging.

    Dies with a pointer at the known backends if the chosen name
    isn't implemented."""
    resolved = name or settings.operation.backend or None
    if resolved is None:
        resolved = _auto_select_backend(settings, prompt=prompt)
    backends = _get_backends(settings)
    if resolved not in backends:
        known = ", ".join(sorted(backends))
        die(f"unknown backend {resolved!r}; known backends: {known}")
    return backends[resolved]


def _platform_vm_suggestion() -> str:
    """Platform-appropriate VM backend name for install suggestions."""
    return "macos-container" if sys.platform == "darwin" else "firecracker"


def _print_vm_install_instructions() -> None:
    """Print platform-appropriate VM backend install instructions to stderr."""
    vm = _platform_vm_suggestion()
    if vm == "macos-container":
        info("Install Apple Container: https://github.com/apple/container/releases")
        info("Then start the service:  container system start")
    else:
        info("Install Firecracker: https://github.com/firecracker-microvm/firecracker/releases")
        info("Configure the host:  bot-bottle backend setup")


def _auto_select_backend(settings: Settings, prompt: bool = True) -> str:
    """Tier-1 / tier-2 backend auto-selection.

    Tier 1: VM backend — macos-container on macOS when Apple Container is
    installed; firecracker on KVM-capable Linux even before the binary is
    present (its preflight prints an install pointer).

    Tier 2: docker, with a security warning and an interactive prompt.
    When `prompt=False` (headless / CI), dies with an actionable message
    instead of blocking on a TTY read. When docker is also absent, prints
    VM install instructions and exits.
    """
    # --- Tier 1: VM backend -----------------------------------------
    if has_backend(settings, "macos-container"):
        return "macos-container"
    # A KVM-capable Linux host defaults to firecracker even when the
    # `firecracker` binary isn't installed yet: selecting it here routes
    # start through firecracker's preflight, which prints an install
    # pointer, instead of silently falling back to docker.
    from .firecracker import FirecrackerBottleBackend
    if FirecrackerBottleBackend.is_host_capable():
        return "firecracker"

    # --- Tier 2: docker fallback ------------------------------------
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
    """Sorted names of every backend. Used by argparse (`--backend` choices)
    and the dashboard's backend picker."""
    return BACKEND_NAMES


def has_backend(settings: Settings, name: str) -> bool:
    """Whether the named backend's runtime prerequisites are
    available on the current host. Cross-backend callers (list,
    cleanup) skip unavailable backends so a docker-only host
    doesn't fail when the firecracker backend isn't usable,
    and vice versa.

    Returns False for unknown names so callers can pass
    arbitrary input without separate validation."""
    backends = _get_backends(settings)
    if name not in backends:
        return False
    return backends[name].is_available()


def is_backend_available(settings: Settings, name: str) -> bool:
    """Cheap availability check: is the backend's binary on PATH?

    Suitable for cleanup enumeration and auto-selection — does NOT probe
    the daemon or network pool. Use is_backend_ready() for a full
    readiness check before launching tests."""
    return has_backend(settings, name)


def is_backend_ready(settings: Settings, name: str, *, quiet: bool = False) -> bool:
    """Full readiness check: passes all of the backend's status() checks.

    When quiet=False the backend prints diagnostic output explaining what
    is missing — intended for test-suite guards that run at discovery time
    so the operator sees a concrete failure reason for each skip.

    Returns False for unknown backend names."""
    backends = _get_backends(settings)
    if name not in backends:
        return False
    return backends[name].status(quiet=quiet) == BackendStatus.READY


def enumerate_active_agents(settings: Settings) -> list[ActiveAgent]:
    """All currently-running agents, across every available
    backend. Used by CLI `active` and the dashboard's agents
    pane so neither has to know which backends exist. Skips
    backends whose `is_available()` reports False.

    Sorted by `(started_at, slug)` so the list is stable across
    dashboard refresh ticks — agents don't shift position while
    the operator navigates with arrow keys. ISO 8601 timestamps
    sort lexicographically in chronological order; `slug` is the
    deterministic tiebreaker. Agents with missing metadata
    (`started_at == ""`) sort first."""
    out: list[ActiveAgent] = []
    backends = _get_backends(settings)
    for name in sorted(backends):
        if not backends[name].is_available():
            continue
        out.extend(backends[name].enumerate_active())
    out.sort(key=lambda a: (a.started_at, a.slug))
    return out
