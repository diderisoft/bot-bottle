"""Active-agent enumeration for the docker backend.

Mirrors `backend/smolmachines/enumerate.py`: returns
`ActiveAgent` records the CLI `list active` command and the
dashboard agents pane consume. Empty when docker isn't reachable
— gated by `has_backend('docker')` at the cross-backend caller
so this module trusts that docker is available when called.

The parser (`_parse_services_by_project`) is exposed for direct
unit testing; the docker `docker ps` invocation is in
`_query_services_by_project`."""

from __future__ import annotations

import subprocess

from .. import ActiveAgent
from .bottle_state import read_metadata
from .compose import compose_project_name, list_active_slugs


def enumerate_active() -> list[ActiveAgent]:
    """All currently-running docker-backed agents. Caller is
    responsible for gating on `has_backend('docker')` if it
    matters; if docker is missing the `docker ps` call below
    returns an empty list silently."""
    slugs = list_active_slugs(include_stopped=False, warn_on_error=False)
    if not slugs:
        return []
    services_by_project = _query_services_by_project()
    out: list[ActiveAgent] = []
    for slug in slugs:
        project = compose_project_name(slug)
        services = services_by_project.get(project, set())
        metadata = read_metadata(slug)
        out.append(ActiveAgent(
            backend_name="docker",
            slug=slug,
            agent_name=metadata.agent_name if metadata else "?",
            started_at=metadata.started_at if metadata else "",
            services=tuple(sorted(services)),
        ))
    return out


def _parse_services_by_project(stdout: str) -> dict[str, set[str]]:
    """Parse `docker ps` output formatted as
    `<project-label>\\t<service-label>` (one line per container)
    into a `{project: {service, ...}}` mapping. Pure function for
    testing — the docker invocation is in `_query_services_by_project`."""
    out: dict[str, set[str]] = {}
    for line in stdout.splitlines():
        project, _, service = line.partition("\t")
        if not project or not service:
            continue
        out.setdefault(project, set()).add(service)
    return out


def _query_services_by_project() -> dict[str, set[str]]:
    """One `docker ps` call → `{project: {service, ...}}`. Used
    by the CLI's `list active` and the dashboard's agents pane —
    one subprocess per refresh tick, not one per bottle."""
    try:
        r = subprocess.run(
            [
                "docker", "ps",
                "--filter", "label=com.docker.compose.project",
                "--format",
                '{{.Label "com.docker.compose.project"}}'
                "\t"
                '{{.Label "com.docker.compose.service"}}',
            ],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return {}
    if r.returncode != 0:
        return {}
    return _parse_services_by_project(r.stdout or "")
