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

  - Compose project: `claude-bottle-<slug>`.
  - Service names (inside the file): `agent`, `pipelock`,
    `egress`, `git-gate`, `supervise`.
  - `container_name:` matches today's pattern
    (`claude-bottle-<service>-<slug>`) so dashboard/cleanup discovery
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
from ...log import die, warn
from ...git_gate import git_gate_aggregate_extra_hosts
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
    EGRESS_DOCKERFILE,
    EGRESS_IMAGE,
    EGRESS_PIPELOCK_CA_IN_CONTAINER,
    egress_container_name,
)
from .git_gate import (
    GIT_GATE_ACCESS_HOOK_IN_CONTAINER,
    GIT_GATE_CREDS_DIR_IN_CONTAINER,
    GIT_GATE_DOCKERFILE,
    GIT_GATE_ENTRYPOINT_IN_CONTAINER,
    GIT_GATE_HOOK_IN_CONTAINER,
    GIT_GATE_IMAGE,
    git_gate_container_name,
)
from .pipelock import (
    PIPELOCK_CA_CERT_IN_CONTAINER,
    PIPELOCK_CA_KEY_IN_CONTAINER,
    PIPELOCK_IMAGE,
    PIPELOCK_PORT,
    pipelock_container_name,
)
from .provision.ca import AGENT_CA_BUNDLE, AGENT_CA_PATH
from .supervise import (
    SUPERVISE_DOCKERFILE,
    SUPERVISE_IMAGE,
    supervise_container_name,
)


# Repo root, used as the build context for sidecar Dockerfiles.
# Same derivation as the per-sidecar lifecycle modules.
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
    project = f"claude-bottle-{plan.slug}"
    services: dict[str, Any] = {}

    services["pipelock"] = _pipelock_service(plan)

    if plan.git_gate_plan.upstreams:
        services["git-gate"] = _git_gate_service(plan)

    if plan.egress_plan.routes:
        services["egress"] = _egress_service(plan)

    if plan.supervise_plan is not None:
        services["supervise"] = _supervise_service(plan)

    services["agent"] = _agent_service(plan)

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


def _pipelock_service(plan: DockerBottlePlan) -> dict[str, Any]:
    """Pipelock sidecar. Pinned-digest image (no build). The
    rendered YAML config + CA cert + key bind-mount in from the
    paths the prepare step laid down on plan.proxy_plan."""
    pp = plan.proxy_plan
    name = pipelock_container_name(plan.slug)
    return {
        "image": PIPELOCK_IMAGE,
        "container_name": name,
        "command": [
            "run",
            "--config", "/etc/pipelock.yaml",
            "--listen", f"0.0.0.0:{PIPELOCK_PORT}",
        ],
        "networks": {
            "internal": {"aliases": [name]},
            "egress": None,
        },
        "volumes": [
            _bind(pp.yaml_path, "/etc/pipelock.yaml"),
            _bind(pp.ca_cert_host_path, PIPELOCK_CA_CERT_IN_CONTAINER),
            _bind(pp.ca_key_host_path, PIPELOCK_CA_KEY_IN_CONTAINER),
        ],
    }


def _git_gate_service(plan: DockerBottlePlan) -> dict[str, Any]:
    """Git-gate sidecar. Built from Dockerfile.git-gate. Entrypoint
    + pre-receive hook + access-hook bind-mount from the stage
    paths the prepare step wrote. Per-upstream identity files
    bind-mount from the user's ssh-key location after `~`
    expansion. Per-upstream known_hosts files come in via chunk 2 —
    the GitGatePlan doesn't carry those host paths yet (they're
    currently materialized at start time by DockerGitGate.start).
    """
    gp = plan.git_gate_plan
    name = git_gate_container_name(plan.slug)

    volumes: list[dict[str, Any]] = [
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

    service: dict[str, Any] = {
        "image": GIT_GATE_IMAGE,
        "build": {
            "context": _REPO_DIR,
            "dockerfile": GIT_GATE_DOCKERFILE,
        },
        "container_name": name,
        "networks": {
            "internal": {"aliases": [name]},
            "egress": None,
        },
        "volumes": volumes,
    }
    extra_hosts = git_gate_aggregate_extra_hosts(gp.upstreams)
    if extra_hosts:
        service["extra_hosts"] = [
            f"{host}:{ip}" for host, ip in sorted(extra_hosts.items())
        ]
    return service


def _egress_service(plan: DockerBottlePlan) -> dict[str, Any]:
    """Egress sidecar. Built from Dockerfile.egress. Routes
    through pipelock on its upstream leg via `EGRESS_UPSTREAM_PROXY` +
    `EGRESS_UPSTREAM_CA`. One env-list entry per upstream-token slot
    (bare NAME inherits from the compose-up process env, so secret
    values stay off argv and out of the compose file). routes.yaml +
    mitmproxy CA + pipelock CA bind-mount from the stage paths."""
    ep = plan.egress_plan
    name = egress_container_name(plan.slug)

    env: list[str] = [
        f"EGRESS_UPSTREAM_PROXY={ep.pipelock_proxy_url}",
        f"HTTPS_PROXY={ep.pipelock_proxy_url}",
        f"HTTP_PROXY={ep.pipelock_proxy_url}",
        "NO_PROXY=localhost,127.0.0.1",
        f"EGRESS_UPSTREAM_CA={EGRESS_PIPELOCK_CA_IN_CONTAINER}",
    ]
    for token_env in sorted(ep.token_env_map.keys()):
        env.append(token_env)

    return {
        "image": EGRESS_IMAGE,
        "build": {
            "context": _REPO_DIR,
            "dockerfile": EGRESS_DOCKERFILE,
        },
        "container_name": name,
        "networks": {
            "internal": {"aliases": [EGRESS_HOSTNAME]},
            "egress": None,
        },
        "environment": env,
        "volumes": [
            _bind(ep.routes_path, EGRESS_ROUTES_IN_CONTAINER),
            _bind(ep.mitmproxy_ca_host_path, EGRESS_CA_IN_CONTAINER),
            _bind(ep.pipelock_ca_host_path, EGRESS_PIPELOCK_CA_IN_CONTAINER),
        ],
        "depends_on": ["pipelock"],
    }


def _supervise_service(plan: DockerBottlePlan) -> dict[str, Any]:
    """Supervise sidecar. Internal network only — no upstream calls.
    Queue dir bind-mounts read-write so the sidecar can append audit
    events and the host-side capability handlers can drop new
    proposals into it."""
    sp = plan.supervise_plan
    assert sp is not None
    name = supervise_container_name(plan.slug)
    return {
        "image": SUPERVISE_IMAGE,
        "build": {
            "context": _REPO_DIR,
            "dockerfile": SUPERVISE_DOCKERFILE,
        },
        "container_name": name,
        "networks": {
            "internal": {"aliases": [SUPERVISE_HOSTNAME]},
        },
        "environment": [
            f"SUPERVISE_BOTTLE_SLUG={plan.slug}",
            f"SUPERVISE_QUEUE_DIR={QUEUE_DIR_IN_CONTAINER}",
            f"SUPERVISE_PORT={SUPERVISE_PORT}",
        ],
        "volumes": [
            {
                "type": "bind",
                "source": str(sp.queue_dir),
                "target": QUEUE_DIR_IN_CONTAINER,
                "read_only": False,
            },
        ],
    }


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

    depends_on = ["pipelock"]
    if plan.git_gate_plan.upstreams:
        depends_on.append("git-gate")
    if plan.egress_plan.routes:
        depends_on.append("egress")
    if plan.supervise_plan is not None:
        depends_on.append("supervise")
    service["depends_on"] = depends_on

    return service


def _agent_proxy_url(plan: DockerBottlePlan) -> str:
    """Pick the agent's HTTP_PROXY. With egress declared, the agent
    goes through egress (which in turn HTTPS_PROXYs to pipelock on
    its outbound leg). Without egress, the agent talks straight to
    pipelock."""
    if plan.egress_plan.routes:
        from .egress import EGRESS_PORT
        return f"http://{EGRESS_HOSTNAME}:{EGRESS_PORT}"
    return f"http://{pipelock_container_name(plan.slug)}:{PIPELOCK_PORT}"


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


def compose_project_name(slug: str) -> str:
    """Stable mapping from slug → compose project. Matches the
    `name:` field the renderer emits, so `docker compose ls`
    enumeration and direct CLI invocations agree on the project
    identifier."""
    return f"claude-bottle-{slug}"


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
    "bottle_plan_to_compose",
    "compose_down",
    "compose_dump_logs",
    "compose_file_path",
    "compose_log_path",
    "compose_project_name",
    "compose_up",
    "write_compose_file",
]
