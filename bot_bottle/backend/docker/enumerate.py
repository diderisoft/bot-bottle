"""Active-agent listing for the docker backend. Query failures raise rather
than read as an empty result."""

from __future__ import annotations

import subprocess

from .. import ActiveAgent, EnumerationError
from ...bottle_state import read_metadata
from .compose import compose_project_name, list_active_slugs


def enumerate_active() -> list[ActiveAgent]:
    """All running docker agents."""
    slugs = list_active_slugs(
        include_stopped=False,
        warn_on_error=False,
        raise_on_error=True,
    )
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
            label=metadata.label if metadata else "",
            color=metadata.color if metadata else "",
        ))
    return out


def _parse_services_by_project(stdout: str) -> dict[str, set[str]]:
    """Parse `docker ps` lines of `<project>\\t<service>` into `{project:
    {service, ...}}`."""
    out: dict[str, set[str]] = {}
    for line in stdout.splitlines():
        project, _, service = line.partition("\t")
        if not project or not service:
            continue
        out.setdefault(project, set()).add(service)
    return out


def _query_services_by_project() -> dict[str, set[str]]:
    """One `docker ps` call -> `{project: {service, ...}}`."""
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
    except OSError as exc:
        # Missing, or not executable by this user.
        raise EnumerationError(f"docker ps failed: docker unavailable ({exc})") from exc
    if r.returncode != 0:
        raise EnumerationError(f"docker ps failed: {r.stderr.strip()}")
    return _parse_services_by_project(r.stdout or "")
