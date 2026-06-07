"""Active-agent enumeration for the smolmachines backend (PRD
0023 chunk 4 follow-up + issue #77).

Returns a list of `ActiveAgent` records — same shape the docker
backend produces — so CLI `list active` and the dashboard agents
pane render both backends through one code path.

A smolmachines agent is "active" when its smolvm guest is
running. We cross-reference against the per-bottle sidecar
bundle container to populate the `services` field (which daemons
are up in the bundle); without a bundle we still surface the VM
so the operator can see + clean it up.

The cross-backend caller gates on `has_backend("smolmachines")`
and `has_backend("docker")`, so this module assumes both are
available when called. Both subprocess calls below still
tolerate "command not on PATH" defensively, but the gate is the
intended access pattern."""

from __future__ import annotations

import json
import subprocess

from .. import ActiveAgent
from ..docker.bottle_state import read_metadata
from . import sidecar_bundle as _bundle


# Smolvm VM names produced by prepare are `bot-bottle-<slug>`,
# matching the bundle container name pattern. We use the prefix
# both as a filter and to strip back to the slug.
_VM_NAME_PREFIX = "bot-bottle-"


def enumerate_active() -> list[ActiveAgent]:
    """All currently-running smolmachines-backed agents. Empty
    list when no matching VMs are running. Caller is responsible
    for gating on `has_backend('smolmachines')` if needed; if
    smolvm is missing the `smolvm machine ls` call below returns
    nothing silently."""
    result = subprocess.run(
        ["smolvm", "machine", "ls", "--json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    try:
        machines = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    services_by_slug = _query_bundle_services()
    out: list[ActiveAgent] = []
    for m in machines:
        name = m.get("name") or ""
        state = m.get("state") or ""
        if state != "running" or not name.startswith(_VM_NAME_PREFIX):
            continue
        slug = name[len(_VM_NAME_PREFIX):]
        metadata = read_metadata(slug)
        out.append(ActiveAgent(
            backend_name="smolmachines",
            slug=slug,
            agent_name=metadata.agent_name if metadata else "?",
            started_at=metadata.started_at if metadata else "",
            services=services_by_slug.get(slug, ()),
            label=metadata.label if metadata else "",
            color=metadata.color if metadata else "",
        ))
    return out


def _query_bundle_services() -> dict[str, tuple[str, ...]]:
    """`{slug: ('egress', ...)}` from each running bundle container's
    `BOT_BOTTLE_SIDECAR_DAEMONS` env var.
    Smolmachines bundles all run the PRD-0024 image with the
    same daemon set declared via env, so one inspect per bundle
    gets us the picture without exec'ing into the container.

    Returns an empty mapping when the docker backend isn't
    available — the bundle services field on each ActiveAgent
    just shows up empty, matching the docker backend's "starting"
    state."""
    # Late import: `has_backend` lives on the backend package's
    # __init__, which imports this module transitively. Pulling
    # the name in at call time sidesteps the cycle.
    from .. import has_backend
    if not has_backend("docker"):
        return {}
    ps = subprocess.run(
        ["docker", "ps",
         "--filter", "name=" + _bundle.bundle_container_name(""),
         "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    if ps.returncode != 0:
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for line in (ps.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        slug = name.removeprefix(_bundle.bundle_container_name(""))
        if not slug:
            continue
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format", "{{json .Config.Env}}"],
            capture_output=True, text=True, check=False,
        )
        if inspect.returncode != 0:
            continue
        try:
            env_list = json.loads(inspect.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for entry in env_list:
            key, _, value = entry.partition("=")
            if key == "BOT_BOTTLE_SIDECAR_DAEMONS":
                out[slug] = tuple(sorted(
                    d for d in value.split(",") if d
                ))
                break
    return out
