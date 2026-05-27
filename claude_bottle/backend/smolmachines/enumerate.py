"""Active-bottle enumeration for the smolmachines backend (PRD
0023 chunk 4 follow-up + issue #77).

Returns a list of `ActiveBottle` records — same shape the docker
backend produces — so CLI `list active` and the dashboard agents
pane render both backends through one code path.

A smolmachines bottle is "active" when its smolvm guest is
running. We cross-reference against the per-bottle sidecar
bundle container to populate the `services` field (which daemons
are up in the bundle); without a bundle we still surface the VM
so the operator can see + clean it up."""

from __future__ import annotations

import json
import shutil
import subprocess

from .. import ActiveBottle
from ..docker.bottle_state import read_metadata
from . import sidecar_bundle as _bundle
from . import smolvm as _smolvm


# Smolvm VM names produced by prepare are `claude-bottle-<slug>`,
# matching the bundle container name pattern. We use the prefix
# both as a filter and to strip back to the slug.
_VM_NAME_PREFIX = "claude-bottle-"


def enumerate_active() -> list[ActiveBottle]:
    """All currently-running smolmachines-backed bottles. Empty
    list when smolvm isn't on PATH or no matching VMs are
    running."""
    if not _smolvm.is_available():
        return []
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
    out: list[ActiveBottle] = []
    for m in machines:
        name = m.get("name") or ""
        state = m.get("state") or ""
        if state != "running" or not name.startswith(_VM_NAME_PREFIX):
            continue
        slug = name[len(_VM_NAME_PREFIX):]
        metadata = read_metadata(slug)
        out.append(ActiveBottle(
            backend_name="smolmachines",
            slug=slug,
            agent_name=metadata.agent_name if metadata else "?",
            started_at=metadata.started_at if metadata else "",
            services=services_by_slug.get(slug, ()),
        ))
    return out


def _query_bundle_services() -> dict[str, tuple[str, ...]]:
    """`{slug: ('egress', 'pipelock', ...)}` from each running
    bundle container's `CLAUDE_BOTTLE_SIDECAR_DAEMONS` env var.
    Smolmachines bundles all run the PRD-0024 image with the
    same daemon set declared via env, so one inspect per bundle
    gets us the picture without exec'ing into the container."""
    if shutil.which("docker") is None:
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
            if key == "CLAUDE_BOTTLE_SIDECAR_DAEMONS":
                out[slug] = tuple(sorted(
                    d for d in value.split(",") if d
                ))
                break
    return out
