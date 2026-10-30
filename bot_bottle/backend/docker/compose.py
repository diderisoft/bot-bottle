"""Docker compose I/O: write a spec (built by `consolidated_compose`), run `up`
and `down`, dump logs, and list `bot-bottle-*` projects.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...environment import resource_prefix
from ...lib.log import die, warn
from ..base import EnumerationError
from .util import daemon_running




COMPOSE_FILE_NAME = "docker-compose.yml"
COMPOSE_LOG_NAME = "compose.log"


COMPOSE_PROJECT_PREFIX = resource_prefix()


def compose_project_name(slug: str) -> str:
    """slug -> compose project name, matching the spec's `name:`."""
    return f"{COMPOSE_PROJECT_PREFIX}{slug}"


def slug_from_compose_project(project: str) -> str:
    """Inverse of `compose_project_name`; "" for a project without the prefix."""
    if not project.startswith(COMPOSE_PROJECT_PREFIX):
        return ""
    return project[len(COMPOSE_PROJECT_PREFIX):]


def list_compose_projects(
    *,
    include_stopped: bool = True,
    warn_on_error: bool = True,
    raise_on_error: bool = False,
) -> list[str]:
    """All `bot-bottle-*` compose project names, stopped ones included unless
    `include_stopped=False`. Errors give `[]` unless `raise_on_error`, so an
    enumeration never mistakes a failed query for an empty one. A daemon that
    isn't running is `[]` either way.
    """
    argv = ["docker", "compose", "ls", "--format", "json"]
    if include_stopped:
        argv.insert(3, "--all")
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        # Not only "not found": a non-executable docker raises PermissionError.
        if raise_on_error:
            raise EnumerationError(
                f"docker compose ls failed: docker unavailable ({exc})"
            ) from exc
        return []
    if result.returncode != 0:
        if not daemon_running():
            return []
        message = f"docker compose ls failed: {result.stderr.strip()}"
        if raise_on_error:
            raise EnumerationError(message)
        if warn_on_error:
            warn(message)
        return []
    try:
        projects = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        message = f"docker compose ls returned malformed JSON: {e}"
        if raise_on_error:
            raise EnumerationError(message) from e
        if warn_on_error:
            warn(message)
        return []
    names: list[str] = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        name = str(p.get("Name", ""))
        if name.startswith(COMPOSE_PROJECT_PREFIX):
            names.append(name)
    return sorted(set(names))


def list_active_slugs(
    *,
    include_stopped: bool = False,
    warn_on_error: bool = True,
    raise_on_error: bool = False,
) -> list[str]:
    """Slugs of running bottles."""
    return sorted(
        slug for slug in (
            slug_from_compose_project(p)
            for p in list_compose_projects(
                include_stopped=include_stopped,
                warn_on_error=warn_on_error,
                raise_on_error=raise_on_error,
            )
        ) if slug
    )


def compose_file_path(state_dir: Path) -> Path:
    return state_dir / COMPOSE_FILE_NAME


def compose_log_path(state_dir: Path) -> Path:
    return state_dir / COMPOSE_LOG_NAME


def write_compose_file(spec: dict[str, Any], path: Path) -> Path:
    """Write the compose dict as JSON (a YAML subset `docker compose` reads)
    with a `.yml` name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n")
    path.chmod(0o644)
    return path


def _compose_argv(project: str, compose_file: Path, *cmd: str) -> list[str]:
    return [
        "docker", "compose",
        "-p", project,
        "-f", str(compose_file),
        *cmd,
    ]


def compose_up(
    project: str,
    compose_file: Path,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """`docker compose up -d`. Bare `environment: [NAME]` entries take their
    value from `env`, so secrets never reach argv or the file."""
    argv = _compose_argv(project, compose_file, "up", "-d")
    result = subprocess.run(
        argv, capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        die(f"docker compose up failed for project {project}")


def compose_dump_logs(project: str, compose_file: Path, output: Path) -> None:
    """Write every service's merged logs to `output`. Best effort."""
    output.parent.mkdir(parents=True, exist_ok=True)
    argv = _compose_argv(project, compose_file, "logs", "--no-color", "--timestamps")
    try:
        with open(output, "wb") as f:
            subprocess.run(
                argv,
                stdout=f,
                stderr=subprocess.STDOUT,
                check=False,
            )
        output.chmod(0o644)
    except OSError as e:
        warn(f"failed to write compose log to {output}: {e}")


def compose_down(project: str, compose_file: Path) -> None:
    """`docker compose down`. External networks aren't removed."""
    argv = _compose_argv(project, compose_file, "down")
    result = subprocess.run(
        argv, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        warn(
            f"docker compose down failed for project {project}: "
            f"{result.stderr.strip()}"
        )


__all__ = [
    "COMPOSE_FILE_NAME",
    "COMPOSE_LOG_NAME",
    "COMPOSE_PROJECT_PREFIX",
    "compose_down",
    "compose_dump_logs",
    "compose_file_path",
    "compose_log_path",
    "compose_project_name",
    "compose_up",
    "list_active_slugs",
    "list_compose_projects",
    "slug_from_compose_project",
    "write_compose_file",
]
