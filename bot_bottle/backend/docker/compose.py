"""Compose-spec rendering for a Docker bottle (PRD 0018, chunk 1).

`bottle_plan_to_compose(plan)` returns a Compose v2 spec dict
describing the per-bottle container topology — one project per
bottle instance, services for the agent + every applicable sidecar,
two networks, no named volumes.

Pure function. No I/O, no subprocess. Expects every launch-time
field (network names, CA host paths, etc.) on the plan's inner
plans to be populated; chunks 2+3 own that ordering. Chunk 1 just
encodes the translation so it can be unit-tested in isolation.

Conditional services follow the plan content (matches the
SDK-call branching in `launch.py` today):

  - pipelock + agent: always.
  - git-gate:         iff plan.git_gate_plan.upstreams.
  - egress:           iff plan.egress_plan.routes.
  - supervise:        iff plan.supervise_plan is not None.

Naming:

  - Compose project: `bot-bottle-<slug>`.
  - Service names (inside the file): `agent`, `pipelock`,
    `egress`, `git-gate`, `supervise`.
  - `container_name:` matches today's pattern
    (`bot-bottle-<service>-<slug>`) so dashboard/cleanup discovery
    via the prefix scan keeps working through the transition.
  - Network aliases preserve the current dial-by-shortname pattern
    for `egress` / `supervise`, and add the long container-name as
    an internal-network alias for `pipelock` / `git-gate` so any
    caller still referencing the long name resolves.

Sidecars that are built (egress, git-gate, supervise) get a
compose `build:` block pointing at the repo Dockerfile; the
`image:` tag is set explicitly so cached images on the daemon
aren't rebuilt on every up.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...egress import (
    EGRESS_HOSTNAME,
    EGRESS_ROUTES_IN_CONTAINER,
)
from ...git_gate import GIT_GATE_HOSTNAME, git_gate_aggregate_extra_hosts
from ...log import die, warn
from ...pipelock import PIPELOCK_HOSTNAME
from ...supervise import (
    CURRENT_CONFIG_DIR_IN_AGENT,
    QUEUE_DIR_IN_CONTAINER,
    SUPERVISE_HOSTNAME,
    SUPERVISE_PORT,
)
from ...util import expand_tilde
from .bottle_plan import DockerBottlePlan
from .egress import (
    EGRESS_CA_IN_CONTAINER,
    EGRESS_PIPELOCK_CA_IN_CONTAINER,
)
from .git_gate import (
    GIT_GATE_ACCESS_HOOK_IN_CONTAINER,
    GIT_GATE_CREDS_DIR_IN_CONTAINER,
    GIT_GATE_ENTRYPOINT_IN_CONTAINER,
    GIT_GATE_HOOK_IN_CONTAINER,
)
from .pipelock import (
    PIPELOCK_CA_CERT_IN_CONTAINER,
    PIPELOCK_CA_KEY_IN_CONTAINER,
    PIPELOCK_PORT,
)
from .provision.ca import AGENT_CA_BUNDLE, AGENT_CA_PATH
from .sidecar_bundle import (
    SIDECAR_BUNDLE_DOCKERFILE,
    SIDECAR_BUNDLE_IMAGE,
    sidecar_bundle_container_name,
)


# Repo root, used as the build context for the bundle Dockerfile.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def bottle_plan_to_compose(plan: DockerBottlePlan) -> dict[str, Any]:
    """Render a Compose v2 spec dict from a fully-resolved
    DockerBottlePlan.

    The plan must have its inner plans (`proxy_plan`,
    `git_gate_plan`, `egress_plan`, `supervise_plan`) populated
    with launch-time fields — network names, CA host paths,
    pipelock_proxy_url. The renderer doesn't validate; callers
    feed it a fully-resolved plan or get an incomplete compose
    spec back.
    """
    project = f"bot-bottle-{plan.slug}"
    services: dict[str, Any] = {
        "sidecars": _sidecar_bundle_service(plan),
        "agent": _agent_service(plan),
    }
    return {
        "name": project,
        "services": services,
        "networks": _networks(plan),
    }


def _networks(plan: DockerBottlePlan) -> dict[str, Any]:
    """Compose-managed networks with explicit `name:` matching the
    existing slug-suffixed convention. Compose creates them on `up`
    and destroys them on `down`. The internal one is `--internal`
    (no default gateway); the egress one is a normal user-defined
    bridge."""
    return {
        "internal": {
            "name": plan.proxy_plan.internal_network,
            "internal": True,
        },
        "egress": {
            "name": plan.proxy_plan.egress_network,
        },
    }


def _bind(host: str | Path, target: str, *, read_only: bool = True) -> dict[str, Any]:
    """One bind-mount entry in the long-form `volumes:` shape.
    Long form is preferred over `host:target:ro` strings because
    it's easier to inspect in tests and survives whitespace in
    host paths."""
    return {
        "type": "bind",
        "source": str(host),
        "target": target,
        "read_only": read_only,
    }


def _sidecar_bundle_service(plan: DockerBottlePlan) -> dict[str, Any]:
    """The `sidecars` service: one container per bottle, bundle
    image, all four daemons under a Python init supervisor.

    Mechanics:

      - Daemon subset narrows via `BOT_BOTTLE_SIDECAR_DAEMONS`
        env. pipelock is always present; egress / git-gate /
        supervise are conditional on the plan.
      - Volumes are the union of the four daemons' bind-mounts,
        preserving the same in-container paths so each daemon
        finds its config / hooks / CA where it expects.
      - Environment is the union of *daemon-private* env vars
        (EGRESS_UPSTREAM_PROXY, SUPERVISE_BOTTLE_SLUG, etc).
        HTTPS_PROXY is NOT propagated here — see the comment in
        egress_entrypoint.sh; setting it at the container level
        would route git-gate's git fetches through pipelock,
        which is wrong.
      - Network aliases register every legacy short/long
        hostname (pipelock, egress, git-gate, supervise plus
        their `bot-bottle-<service>-<slug>` long forms) so
        the agent's HTTPS_PROXY URL and any other inter-service
        reference resolves to the bundle.
    """
    daemons: list[str] = ["egress", "pipelock"]
    if plan.git_gate_plan.upstreams:
        daemons.append("git-gate")
    if plan.supervise_plan is not None:
        daemons.append("supervise")

    env: list[str] = [f"BOT_BOTTLE_SIDECAR_DAEMONS={','.join(daemons)}"]
    volumes: list[dict[str, Any]] = []

    # --- pipelock ----------------------------------------------------
    pp = plan.proxy_plan
    volumes += [
        _bind(pp.yaml_path, "/etc/pipelock.yaml"),
        _bind(pp.ca_cert_host_path, PIPELOCK_CA_CERT_IN_CONTAINER),
        _bind(pp.ca_key_host_path, PIPELOCK_CA_KEY_IN_CONTAINER),
    ]

    # --- egress (always part of the bundle; the EGRESS_UPSTREAM_*
    # env vars + ca bind-mounts are needed iff routes exist; when
    # the bottle has no routes the egress daemon falls back to its
    # `regular@9099` mode and is unused) -----------------------------
    ep = plan.egress_plan
    if ep.routes:
        env.append(f"EGRESS_UPSTREAM_PROXY={ep.pipelock_proxy_url}")
        env.append(f"EGRESS_UPSTREAM_CA={EGRESS_PIPELOCK_CA_IN_CONTAINER}")
        volumes += [
            _bind(ep.routes_path, EGRESS_ROUTES_IN_CONTAINER),
            _bind(ep.mitmproxy_ca_host_path, EGRESS_CA_IN_CONTAINER),
            _bind(ep.pipelock_ca_host_path, EGRESS_PIPELOCK_CA_IN_CONTAINER),
        ]
        for token_env in sorted(ep.token_env_map.keys()):
            env.append(token_env)

    # --- git-gate ----------------------------------------------------
    extra_hosts: list[str] = []
    gp = plan.git_gate_plan
    if gp.upstreams:
        volumes += [
            _bind(gp.entrypoint_script, GIT_GATE_ENTRYPOINT_IN_CONTAINER),
            _bind(gp.hook_script, GIT_GATE_HOOK_IN_CONTAINER),
            _bind(gp.access_hook_script, GIT_GATE_ACCESS_HOOK_IN_CONTAINER),
        ]
        for u in gp.upstreams:
            keypath = expand_tilde(u.identity_file)
            volumes.append(_bind(
                keypath,
                f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{u.name}-key",
            ))
            if u.known_hosts_file:
                volumes.append(_bind(
                    u.known_hosts_file,
                    f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{u.name}-known_hosts",
                ))
        extra_map = git_gate_aggregate_extra_hosts(gp.upstreams)
        extra_hosts = [f"{host}:{ip}" for host, ip in sorted(extra_map.items())]

    # --- supervise ---------------------------------------------------
    sp = plan.supervise_plan
    if sp is not None:
        env += [
            f"SUPERVISE_BOTTLE_SLUG={plan.slug}",
            f"SUPERVISE_QUEUE_DIR={QUEUE_DIR_IN_CONTAINER}",
            f"SUPERVISE_PORT={SUPERVISE_PORT}",
        ]
        volumes.append({
            "type": "bind",
            "source": str(sp.queue_dir),
            "target": QUEUE_DIR_IN_CONTAINER,
            "read_only": False,
        })

    # Internal-network aliases: the agent reaches each daemon through
    # its short name (pipelock / egress / git-gate / supervise) which
    # the bundle answers as if it were the daemon itself.
    internal_aliases = [
        PIPELOCK_HOSTNAME,
        EGRESS_HOSTNAME,
    ]
    if gp.upstreams:
        internal_aliases.append(GIT_GATE_HOSTNAME)
    if sp is not None:
        internal_aliases.append(SUPERVISE_HOSTNAME)

    service: dict[str, Any] = {
        "image": SIDECAR_BUNDLE_IMAGE,
        "build": {
            "context": _REPO_DIR,
            "dockerfile": SIDECAR_BUNDLE_DOCKERFILE,
        },
        "container_name": sidecar_bundle_container_name(plan.slug),
        "networks": {
            "internal": {"aliases": internal_aliases},
            "egress": None,
        },
        "environment": env,
        "volumes": volumes,
    }
    if extra_hosts:
        service["extra_hosts"] = extra_hosts
    return service


def _agent_service(plan: DockerBottlePlan) -> dict[str, Any]:
    """Agent container. Runs `sleep infinity`; claude is `docker
    exec -it`'d into it later. No TTY at the container level —
    interactivity is per-exec. HTTP_PROXY/HTTPS_PROXY point at the
    egress short-alias when an egress is declared, otherwise
    straight at pipelock's container name. CA trust trio matches
    the existing launch.py wiring."""
    proxy_url = _agent_proxy_url(plan)
    no_proxy = _agent_no_proxy(plan)
    env: list[str] = [
        f"HTTPS_PROXY={proxy_url}",
        f"HTTP_PROXY={proxy_url}",
        f"https_proxy={proxy_url}",
        f"http_proxy={proxy_url}",
        f"NO_PROXY={no_proxy}",
        f"no_proxy={no_proxy}",
        f"NODE_EXTRA_CA_CERTS={AGENT_CA_PATH}",
        f"SSL_CERT_FILE={AGENT_CA_BUNDLE}",
        f"REQUESTS_CA_BUNDLE={AGENT_CA_BUNDLE}",
    ]
    # Forwarded vars (OAuth token, manifest host-interpolations):
    # bare name → inherits from compose-up process env, value
    # never lands on argv or in the compose file.
    for name in sorted(plan.forwarded_env.keys()):
        env.append(name)

    service: dict[str, Any] = {
        "image": plan.runtime_image,
        "container_name": plan.container_name,
        "command": ["sleep", "infinity"],
        "networks": {"internal": None},
        "environment": env,
    }
    if plan.use_runsc:
        service["runtime"] = "runsc"
    if plan.env_file and plan.env_file.exists() and plan.env_file.stat().st_size > 0:
        service["env_file"] = [str(plan.env_file)]

    volumes: list[dict[str, Any]] = []
    if plan.supervise_plan is not None:
        volumes.append(_bind(
            plan.supervise_plan.current_config_dir,
            CURRENT_CONFIG_DIR_IN_AGENT,
        ))
    if volumes:
        service["volumes"] = volumes

    # The init supervisor inside the bundle owns intra-bundle
    # daemon ordering, so the agent only waits for the bundle
    # container itself.
    service["depends_on"] = ["sidecars"]

    return service


def _agent_proxy_url(plan: DockerBottlePlan) -> str:
    """Pick the agent's HTTP_PROXY. With egress declared, the agent
    goes through egress (which in turn HTTPS_PROXYs to pipelock on
    its outbound leg). Without egress, the agent talks straight to
    pipelock."""
    if plan.egress_plan.routes:
        from .egress import EGRESS_PORT
        return f"http://{EGRESS_HOSTNAME}:{EGRESS_PORT}"
    return f"http://{PIPELOCK_HOSTNAME}:{PIPELOCK_PORT}"


def _agent_no_proxy(plan: DockerBottlePlan) -> str:
    """NO_PROXY for the agent. Matches the launch.py rules:
    loopback always, supervise hostname when the supervise sidecar
    is up (the MCP long-poll pattern needs to bypass pipelock's
    idle timeout)."""
    hosts = ["localhost", "127.0.0.1"]
    if plan.supervise_plan is not None:
        hosts.append(SUPERVISE_HOSTNAME)
    return ",".join(hosts)


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
    *, include_stopped: bool = True, warn_on_error: bool = True,
) -> list[str]:
    """All compose project names starting with `bot-bottle-`.
    `include_stopped=True` (default) runs `docker compose ls --all`
    so exited projects appear too; pass False to get only projects
    with at least one running container.

    Returns [] on docker daemon errors or malformed output rather
    than raising — callers should treat the empty list as "no
    projects discoverable", not "no projects exist". `warn_on_error`
    stays true for explicit operator commands like cleanup, but active
    discovery paths set it false so dashboard refreshes don't spam
    stderr while Docker Desktop is stopped."""
    argv = ["docker", "compose", "ls", "--format", "json"]
    if include_stopped:
        argv.insert(3, "--all")
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        # docker binary not on PATH — same shape as a daemon-down
        # error from the caller's POV: no projects discoverable.
        return []
    if result.returncode != 0:
        if warn_on_error:
            warn(f"docker compose ls failed: {result.stderr.strip()}")
        return []
    try:
        projects = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        if warn_on_error:
            warn(f"docker compose ls returned malformed JSON: {e}")
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
    *, include_stopped: bool = False, warn_on_error: bool = True,
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
    "bottle_plan_to_compose",
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
