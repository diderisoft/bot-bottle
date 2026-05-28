"""Cleanup + active-listing for the smolmachines backend (issue #77).

`prepare_cleanup` enumerates leftover smolmachines resources:

  - smolvm machines (`smolvm machine ls --json`) whose name starts
    with `bot-bottle-`.
  - bundle docker containers (`bot-bottle-sidecars-<slug>`).
  - bundle docker networks (`bot-bottle-bundle-<slug>`).

State dirs live under `~/.bot-bottle/state/<identity>/` —
shared layout with the docker backend, which has the single
orphan-state-dir enumerator (it already consults
`enumerate_active_agents()` so a live smolmachines bottle's dir
is preserved).

`cleanup` removes everything in the plan: stop + delete each VM,
force-rm each container, rm each network. Each step is
best-effort — a failure on one resource doesn't block the others."""

from __future__ import annotations

import json
import subprocess

from ...log import info, warn
from . import sidecar_bundle as _bundle
from . import smolvm as _smolvm
from .bottle_cleanup_plan import SmolmachinesBottleCleanupPlan


# Both names start with the same prefix the launcher uses.
_VM_PREFIX = "bot-bottle-"
_BUNDLE_PREFIX = _bundle.bundle_container_name("")          # `bot-bottle-sidecars-`
_NETWORK_PREFIX = _bundle.bundle_network_name("")           # `bot-bottle-bundle-`


def prepare_cleanup() -> SmolmachinesBottleCleanupPlan:
    """Enumerate every smolmachines-owned resource on the host.
    No side effects. Returns an empty plan when smolvm isn't on
    PATH (no machines to reap) — `cleanup` is a no-op in that
    case too."""
    machines = _list_bot_bottle_machines()
    bundles = _list_bundle_containers()
    networks = _list_bundle_networks()
    return SmolmachinesBottleCleanupPlan(
        machines=tuple(sorted(machines)),
        bundles=tuple(sorted(bundles)),
        networks=tuple(sorted(networks)),
    )


def cleanup(plan: SmolmachinesBottleCleanupPlan) -> None:
    """Remove everything in the plan. Order matters: stop VMs
    first (they hold ports on lo0 aliases via libkrun), then the
    bundle containers (which hold the host port-forwards), then
    the networks (which docker won't reap until the containers
    are gone)."""
    for name in plan.machines:
        info(f"stopping smolvm machine {name}")
        subprocess.run(
            ["smolvm", "machine", "stop", "--name", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        info(f"deleting smolvm machine {name}")
        r = subprocess.run(
            ["smolvm", "machine", "delete", "-f", name],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            warn(
                f"smolvm machine delete -f {name} failed: "
                f"{(r.stderr or '').strip()}"
            )

    for name in plan.bundles:
        info(f"removing bundle container {name}")
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )

    for name in plan.networks:
        info(f"removing bundle network {name}")
        r = subprocess.run(
            ["docker", "network", "rm", name],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0 and "no such network" not in (r.stderr or "").lower():
            warn(
                f"docker network rm {name} failed: "
                f"{(r.stderr or '').strip()}"
            )


def _list_bot_bottle_machines() -> list[str]:
    """All smolvm machines named `bot-bottle-*`, regardless of
    state (running / stopped / created). Empty when smolvm isn't
    installed."""
    if not _smolvm.is_available():
        return []
    r = subprocess.run(
        ["smolvm", "machine", "ls", "--json"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return []
    try:
        machines = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        m["name"] for m in machines
        if isinstance(m, dict)
        and m.get("name", "").startswith(_VM_PREFIX)
    ]


def _list_bundle_containers() -> list[str]:
    """All docker containers named `bot-bottle-sidecars-*`,
    running or stopped. Empty when docker isn't installed."""
    # Late import: `backend/__init__` imports this module
    # transitively via the smolmachines backend.
    from .. import has_backend
    if not has_backend("docker"):
        return []
    r = subprocess.run(
        ["docker", "ps", "-a",
         "--filter", f"name=^{_BUNDLE_PREFIX}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return []
    return [
        line for line in (r.stdout or "").splitlines()
        if line and line.startswith(_BUNDLE_PREFIX)
    ]


def _list_bundle_networks() -> list[str]:
    """All docker networks named `bot-bottle-bundle-*`. Empty
    when docker isn't installed."""
    from .. import has_backend
    if not has_backend("docker"):
        return []
    r = subprocess.run(
        ["docker", "network", "ls",
         "--filter", f"name={_NETWORK_PREFIX}",
         "--format", "{{.Name}}"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return []
    return [
        line for line in (r.stdout or "").splitlines()
        if line and line.startswith(_NETWORK_PREFIX)
    ]
