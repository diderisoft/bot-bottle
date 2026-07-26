"""Docker compose lifecycle helpers (PRD 0018).

Serialize a compose spec to disk, drive `docker compose up/down`,
dump the merged log on teardown, and enumerate `bot-bottle-*`
projects. The spec itself is built by `consolidated_compose.py`
(the consolidated per-host gateway topology); this module owns the
I/O side that persists and runs it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...log import die, warn


# --- Lifecycle helpers (PRD 0018 chunk 3) ----------------------------------
#
# The renderer above is pure. The helpers below own the I/O side:
# serialize the spec to disk, drive `docker compose up`, dump the
# merged log file on teardown, and `docker compose down` to clean up
# (networks are pre-created externally so `down` leaves them alone;
# the launch step removes them in its own teardown step).


COMPOSE_FILE_NAME = "docker-compose.yml"
COMPOSE_LOG_NAME = "compose.log"


COMPOSE_PROJECT_PREFIX = "bot-bottle-"


def compose_project_name(slug: str) -> str:
    """Stable mapping from slug → compose project. Matches the
    `name:` field the renderer emits, so `docker compose ls`
    enumeration and direct CLI invocations agree on the project
    identifier."""
    return f"{COMPOSE_PROJECT_PREFIX}{slug}"


def slug_from_compose_project(project: str) -> str:
    """Inverse of `compose_project_name`: strip the prefix to get
    the underlying slug. Returns empty string if the project name
    doesn't start with the expected prefix."""
    if not project.startswith(COMPOSE_PROJECT_PREFIX):
        return ""
    return project[len(COMPOSE_PROJECT_PREFIX):]


def list_compose_projects(
    *,
    include_stopped: bool = True,
    warn_on_error: bool = True,
    raise_on_error: bool = False,
) -> list[str]:
    """All compose project names starting with `bot-bottle-`.
    `include_stopped=True` (default) runs `docker compose ls --all`
    so exited projects appear too; pass False to get only projects
    with at least one running container.

    Best-effort callers get ``[]`` on Docker errors or malformed output.
    Enumeration callers pass ``raise_on_error=True`` so a failed query is not
    reported as an authoritative empty result.
    """
    argv = ["docker", "compose", "ls", "--format", "json"]
    if include_stopped:
        argv.insert(3, "--all")
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        if raise_on_error:
            raise RuntimeError("docker compose ls failed: docker not found") from exc
        return []
    if result.returncode != 0:
        message = f"docker compose ls failed: {result.stderr.strip()}"
        if raise_on_error:
            raise RuntimeError(message)
        if warn_on_error:
            warn(message)
        return []
    try:
        projects = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        message = f"docker compose ls returned malformed JSON: {e}"
        if raise_on_error:
            raise RuntimeError(message) from e
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
    """Slugs (project name minus prefix) of currently-running
    bottles. Used by the dashboard's operator-edit verbs to choose
    a bottle to apply a config edit to."""
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
    """Serialize the compose dict to disk. JSON content with a
    `.yml` filename — JSON is a strict subset of YAML 1.2 for the
    constructs the renderer uses (mappings, lists, strings, bools,
    nulls), and `docker compose -f file.yml` parses it as YAML.
    Avoids a yaml dependency while keeping the file `cat`-readable.
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
    """`docker compose up -d` for the project. Env-inheritance is
    via `env=` on the subprocess — every `environment: [NAME]` (bare
    name) entry in the compose file resolves to whatever value
    `NAME` has in `env` at exec time. Secrets never land on argv or
    in the compose file."""
    argv = _compose_argv(project, compose_file, "up", "-d")
    result = subprocess.run(
        argv, capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        die(f"docker compose up failed for project {project}")


def compose_dump_logs(project: str, compose_file: Path, output: Path) -> None:
    """Write the merged stdout/stderr of every service to `output`
    using `docker compose logs --no-color --timestamps`. Best-effort
    — failures here shouldn't block teardown. The interleaved single
    file is what the user reads post-mortem; per-service tail still
    works through `docker compose logs -f <service>` while the
    project is up."""
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
    """`docker compose down` for the project. External networks are
    intentionally NOT removed by compose (`external: true` on the
    networks block); the launch step's own teardown removes them
    via `network_remove` so the per-bottle ephemeral subnet doesn't
    accumulate."""
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
