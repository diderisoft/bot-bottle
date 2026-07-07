"""End-to-end launch flow for the smolmachines backend
(PRD 0023 chunks 2d + 4b).

Brings up the per-bottle docker bridge + sidecar bundle (with
real daemons + their config files), creates + starts the smolvm
guest pointed at the bundle's pinned IP via TSI's
`--allow-cidr <bundle-ip>/32` allowlist, yields a
`SmolmachinesBottle` handle, tears everything down on context
exit.

The bundle's daemons consume the inner Plans the docker backend
already produces: egress reads routes + CAs from the EgressPlan.
Git-gate + supervise plumb through the same plans the docker
backend uses, minus the docker-network fields that don't apply here."""

from __future__ import annotations

import dataclasses
import os
import platform
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Generator

from ...egress import (
    EGRESS_ROUTES_IN_CONTAINER,
    egress_agent_env_entries,
    egress_resolve_token_values,
    egress_sidecar_env_entries,
)
from ...supervise import DB_PATH_IN_CONTAINER, SUPERVISE_PORT
from ...util import expand_tilde
from ..docker import util as docker_mod
from ..docker.egress import (
    EGRESS_CA_IN_CONTAINER,
    EGRESS_PORT as _EGRESS_PORT,
    egress_tls_init,
)
from ..docker.git_gate import (
    GIT_GATE_ACCESS_HOOK_IN_CONTAINER,
    GIT_GATE_CREDS_DIR_IN_CONTAINER,
    GIT_GATE_ENTRYPOINT_IN_CONTAINER,
    GIT_GATE_HOOK_IN_CONTAINER,
)
from ...git_gate import (
    provision_git_gate_dynamic_keys,
    revoke_git_gate_provisioned_keys,
)
from ...log import info, warn
from ...bottle_state import (
    egress_state_dir,
    git_gate_state_dir,
    read_committed_image,
)
from . import loopback_alias as _loopback
from . import sidecar_bundle as _bundle
from . import smolvm as _smolvm
from .bottle import SmolmachinesBottle
from .bottle_plan import SmolmachinesBottlePlan
from .local_registry import crane_push_tarball, ephemeral_registry


# Repo root, used as the `docker build` context for the agent image.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


# Per-host cache for `smolvm pack create` outputs. Keyed by the
# docker image ID so a Dockerfile change automatically invalidates
# the cache. `pack create` is idempotent on the smolvm side but
# takes several seconds even on a no-op rebuild.
_SMOLMACHINE_CACHE_DIR = Path.home() / ".cache" / "bot-bottle" / "smolmachines"


# Container-internal listening ports for each bundle daemon. The
# bundle publishes each one on a random host loopback port (see
# `_bundle.start_bundle`), and `_bundle.bundle_host_port` looks
# them up post-start.
_GIT_HTTP_PORT = 9420
_SUPERVISE_PORT = SUPERVISE_PORT


@contextmanager
def launch(
    plan: SmolmachinesBottlePlan,
    *,
    provision: Callable[[SmolmachinesBottlePlan, "SmolmachinesBottle"], str | None],
) -> Generator[SmolmachinesBottle, None, None]:
    """Build + run the bottle and yield a handle; tear everything
    down on exit. Errors during bringup unwind any partial state
    via the ExitStack."""
    stack = ExitStack()
    try:
        loopback_ip, network = _allocate_resources(plan, stack)
        plan = _mint_certs(plan)
        proxy_host = _proxy_host(plan, loopback_ip)
        plan = _start_bundle(plan, network, proxy_host, stack)
        plan = _discover_urls(plan, proxy_host)

        agent_from_path = _agent_from_path(plan)

        _launch_vm(plan, agent_from_path, proxy_host, stack)
        _init_vm(plan)

        bottle = SmolmachinesBottle(
            plan.machine_name,
            prompt_path=None,
            guest_env=plan.guest_env,
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            agent_provider_template=plan.agent_provider_template,
            terminal_title=f"{plan.spec.label} ({plan.spec.agent_name})" if plan.spec.label else plan.spec.agent_name,
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
        )
        bottle.prompt_path = provision(plan, bottle)

        yield bottle
    finally:
        _teardown_smolmachines(stack, plan)


def _teardown_smolmachines(
    stack: ExitStack,
    plan: SmolmachinesBottlePlan,
) -> None:
    """Unwind the ExitStack, then revoke any provisioned deploy keys.

    ExitStack errors are caught and logged (non-fatal) so that key
    revocation always runs. Revocation errors propagate — a stranded
    deploy key is a security concern the operator must address."""
    teardown_exc: BaseException | None = None
    try:
        stack.close()
    except BaseException as exc:  # noqa: W0718 — teardown must not fail
        teardown_exc = exc
        warn(f"smolmachines teardown failed: {exc!r}")
    bottle = plan.manifest.bottle
    revoke_git_gate_provisioned_keys(bottle, git_gate_state_dir(plan.slug))
    if teardown_exc is not None:
        raise teardown_exc


def _allocate_resources(
    plan: SmolmachinesBottlePlan,
    stack: ExitStack,
) -> tuple[str, str]:
    """Reserve a loopback alias and create the per-bottle docker bridge.

    The per-bottle alias scopes TSI's allowlist to this bottle's
    published ports so the agent can't reach other bottles' or host
    services' ports on loopback. On macOS `ensure_pool` first
    sudo-aliases the pool on `lo0`; on Linux that's a no-op since
    all of 127.0.0.0/8 is already loopback, but the per-bottle
    allocation runs on both."""
    _loopback.ensure_pool()
    loopback_ip = _loopback.allocate(plan.slug)
    network = _bundle.bundle_network_name(plan.slug)
    _bundle.create_bundle_network(network, plan.bundle_subnet, plan.bundle_gateway)
    stack.callback(_bundle.remove_bundle_network, network)
    return loopback_ip, network


def _mint_certs(plan: SmolmachinesBottlePlan) -> SmolmachinesBottlePlan:
    """Mint the egress MITM CA and return the plan with CA paths filled."""
    egress_ca_host, egress_ca_cert_only = egress_tls_init(
        egress_state_dir(plan.slug),
    )
    egress_plan = dataclasses.replace(
        plan.egress_plan,
        mitmproxy_ca_host_path=egress_ca_host,
        mitmproxy_ca_cert_only_host_path=egress_ca_cert_only,
    )
    return dataclasses.replace(plan, egress_plan=egress_plan)


def _start_bundle(
    plan: SmolmachinesBottlePlan,
    network: str,
    proxy_host: str,
    stack: ExitStack,
) -> SmolmachinesBottlePlan:
    """Build the BundleLaunchSpec, resolve token env, start the
    sidecar bundle container, and register teardown."""
    plan = _provision_git_gate_keys(plan)
    bundle_spec = _bundle_launch_spec(plan, network, proxy_host)
    token_env = _resolve_token_env(plan, dict(os.environ))
    _bundle.ensure_bundle_image(bundle_spec.image)
    _bundle.start_bundle(bundle_spec, env={**os.environ, **token_env})
    stack.callback(_bundle.stop_bundle, plan.slug)
    return plan


def _provision_git_gate_keys(
    plan: SmolmachinesBottlePlan,
) -> SmolmachinesBottlePlan:
    if not plan.git_gate_plan.upstreams:
        return plan
    git_gate_plan = provision_git_gate_dynamic_keys(
        plan.manifest.bottle,
        plan.git_gate_plan,
        git_gate_state_dir(plan.slug),
    )
    return dataclasses.replace(plan, git_gate_plan=git_gate_plan)


def _discover_urls(
    plan: SmolmachinesBottlePlan,
    proxy_host: str,
) -> SmolmachinesBottlePlan:
    """Discover host-side ports for published container ports and
    return the plan with URLs + guest_env stamped in.

    `proxy_host` is the host IP that both TSI's allowlist and
    docker's port-forward bindings are keyed to. On macOS it is the
    per-bottle loopback alias; on Linux it is the per-bottle bridge
    gateway (see `_proxy_host`). The agent dials the published port
    on this IP for all bundle-hosted services.

    NO_PROXY includes `proxy_host` so supervise + git-gate URLs
    bypass HTTPS_PROXY."""
    agent_facing_host_port = _bundle.bundle_host_port(
        plan.slug, _EGRESS_PORT, host_ip=proxy_host,
    )
    agent_proxy_url = f"http://{proxy_host}:{agent_facing_host_port}"

    agent_git_gate_host = ""
    if plan.git_gate_plan.upstreams:
        git_gate_host_port = _bundle.bundle_host_port(
            plan.slug, _GIT_HTTP_PORT, host_ip=proxy_host,
        )
        agent_git_gate_host = f"{proxy_host}:{git_gate_host_port}"

    agent_supervise_url = ""
    if plan.supervise_plan is not None:
        supervise_host_port = _bundle.bundle_host_port(
            plan.slug, _SUPERVISE_PORT, host_ip=proxy_host,
        )
        agent_supervise_url = f"http://{proxy_host}:{supervise_host_port}/"

    existing_no_proxy = plan.guest_env.get("NO_PROXY", "localhost,127.0.0.1")
    no_proxy = f"{existing_no_proxy},{proxy_host}"
    guest_env = {
        **plan.guest_env,
        "HTTPS_PROXY": agent_proxy_url,
        "HTTP_PROXY":  agent_proxy_url,
        "https_proxy": agent_proxy_url,
        "http_proxy":  agent_proxy_url,
        "NO_PROXY":    no_proxy,
        "no_proxy":    no_proxy,
    }
    if agent_git_gate_host:
        guest_env["GIT_GATE_URL"] = f"http://{agent_git_gate_host}"
    if agent_supervise_url:
        guest_env["MCP_SUPERVISE_URL"] = agent_supervise_url
    for entry in egress_agent_env_entries(plan.egress_plan):
        name, value = entry.split("=", 1)
        guest_env[name] = value

    return dataclasses.replace(
        plan,
        guest_env=guest_env,
        agent_proxy_url=agent_proxy_url,
        agent_git_gate_host=agent_git_gate_host,
        agent_supervise_url=agent_supervise_url,
    )


def _launch_vm(
    plan: SmolmachinesBottlePlan,
    agent_from_path: Path,
    proxy_host: str,
    stack: ExitStack,
) -> None:
    """Create, patch, and start the smolvm VM; register teardown.

    --allow-cidr is `proxy_host/32` — the per-bottle loopback alias
    on macOS or the bridge gateway on Linux (see `_proxy_host`). This
    ensures the guest can only reach bundle ports published on that IP,
    not the container IP directly. force_allowlist confirms the
    allowlist persisted (patching smolvm 0.8.0's silent-drop of
    --allow-cidr when combined with --from) and fails closed if it
    can't. Smolfile isn't usable here — smolvm 0.8.0 makes --from
    and --smolfile mutually exclusive."""
    tsi_cidr = f"{proxy_host}/32"
    _smolvm.machine_create(
        plan.machine_name,
        from_path=agent_from_path,
        allow_cidrs=[tsi_cidr],
        env=plan.guest_env,
    )
    stack.callback(_smolvm.machine_delete, plan.machine_name)
    # Confirm the booted VM's TSI allowlist will actually enforce the
    # /32 before start (smolvm 0.8.0 silently drops `--allow-cidr`
    # with `--from`, so the persisted state DB is patched if needed).
    # Fails closed if enforcement can't be confirmed.
    _loopback.force_allowlist(plan.machine_name, [tsi_cidr])
    _smolvm.machine_start(plan.machine_name)
    stack.callback(_smolvm.machine_stop, plan.machine_name)


def _init_vm(plan: SmolmachinesBottlePlan) -> None:
    """Repair filesystem ownership and wait for exec channel readiness.

    Ownership repair: smolvm's pack process remaps files to the host
    invoker's uid (e.g. 501 on macOS, 1000 on Linux). The chowns use
    names not numbers so they're correct on either. /home/node must
    be node:node so
    Claude Code can write ~/.claude.json; /tmp + /var/tmp need root
    mode 1777 so non-root processes can create per-uid scratch dirs.
    All folded into one sh -c to avoid back-to-back exec calls
    immediately after machine_start (libkrun exec-channel race).

    mkdir -p guards: when booting from a committed snapshot, /tmp and
    /var/tmp are excluded from the archive (they're ephemeral and their
    stale contents would have wrong uid after smolvm's uid remap). The
    directories must be created before chown/chmod can set permissions.

    wait_exec_ready polls until the exec channel is ready for the
    subsequent provision calls, replacing the empirical sleep."""
    _smolvm.machine_exec(plan.machine_name, [
        "sh", "-c",
        "mkdir -p /tmp /var/tmp && "
        "chown -R node:node /home/node && "
        "chown root:root /tmp /var/tmp && "
        "chmod 1777 /tmp /var/tmp",
    ])
    _smolvm.wait_exec_ready(plan.machine_name)


def _proxy_host(plan: SmolmachinesBottlePlan, loopback_ip: str) -> str:
    """Return the host IP for TSI's allowlist and docker port-forward bindings.

    On macOS, the per-bottle loopback alias (e.g. ``127.0.0.16``) works
    because macOS's network stack lets TSI intercept 127.x.x.x connects
    from the guest before they reach the host's own loopback.

    On Linux, the guest kernel's LOCAL routing table routes all
    ``127.0.0.0/8`` to the guest's own loopback — those packets never
    reach eth0 and TSI never sees them. Using the per-bottle bridge
    gateway (e.g. ``192.168.N.1``) instead sidesteps the problem: it
    is not a loopback address, so the guest routes it via eth0 and TSI
    intercepts it normally. The TSI allowlist is ``gateway/32``, which
    is distinct from the container IP (``192.168.N.2``), so the agent
    still can't reach the egress daemon directly — TSI blocks any
    connection to the container IP that isn't via the published port."""
    if platform.system() == "Linux":
        return plan.bundle_gateway
    return loopback_ip


def _bundle_launch_spec(
    plan: SmolmachinesBottlePlan, network: str, proxy_host: str,
) -> _bundle.BundleLaunchSpec:
    """Build a BundleLaunchSpec from the resolved inner Plans.

    Daemons in the CSV:
      - egress is always present.
      - git-gate + git-http are conditional on plan.git_gate_plan.upstreams.
      - supervise is conditional on plan.supervise_plan.

    Env + volumes are the union of the sidecar daemons' needs, with
    daemon-private values only (HTTPS_PROXY is scoped to the
    egress process by egress_entrypoint.sh — see PRD 0024's bundle
    bind-address PR)."""
    daemons: list[str] = ["egress"]
    env: list[str] = []
    volumes: list[tuple[str, str, bool]] = []

    # --- egress -----------------------------------------------
    ep = plan.egress_plan
    volumes.append((str(ep.mitmproxy_ca_host_path), EGRESS_CA_IN_CONTAINER, True))
    if ep.routes:
        volumes.append((str(ep.routes_path.parent), str(Path(EGRESS_ROUTES_IN_CONTAINER).parent), True))
    env.extend(egress_sidecar_env_entries(ep))

    # --- git-gate ---------------------------------------------
    gp = plan.git_gate_plan
    if gp.upstreams:
        daemons += ["git-gate", "git-http"]
        volumes += [
            (str(gp.entrypoint_script), GIT_GATE_ENTRYPOINT_IN_CONTAINER, True),
            (str(gp.hook_script), GIT_GATE_HOOK_IN_CONTAINER, True),
            (str(gp.access_hook_script), GIT_GATE_ACCESS_HOOK_IN_CONTAINER, True),
        ]
        for u in gp.upstreams:
            keypath = expand_tilde(u.identity_file)
            volumes.append((
                keypath,
                f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{u.name}-key",
                True,
            ))
            if u.known_hosts_file:
                volumes.append((
                    str(u.known_hosts_file),
                    f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{u.name}-known_hosts",
                    True,
                ))

    # --- supervise --------------------------------------------
    sp = plan.supervise_plan
    if sp is not None:
        daemons.append("supervise")
        env += [
            f"SUPERVISE_BOTTLE_SLUG={plan.slug}",
            f"SUPERVISE_DB_PATH={DB_PATH_IN_CONTAINER}",
            f"SUPERVISE_PORT={SUPERVISE_PORT}",
        ]
        volumes.append((str(sp.db_path), DB_PATH_IN_CONTAINER, False))

    # Container ports the agent reaches from the smolvm guest —
    # published on `proxy_host` so the TSI allowlist and the docker
    # port-forward bindings point at the same IP. Egress is always
    # the agent's HTTP/HTTPS proxy.
    ports_to_publish: list[int] = [_EGRESS_PORT]
    if gp.upstreams:
        ports_to_publish.append(_GIT_HTTP_PORT)
    if sp is not None:
        ports_to_publish.append(_SUPERVISE_PORT)

    return _bundle.BundleLaunchSpec(
        slug=plan.slug,
        network_name=network,
        subnet=plan.bundle_subnet,
        gateway=plan.bundle_gateway,
        bundle_ip=plan.bundle_ip,
        daemons_csv=",".join(daemons),
        environment=tuple(env),
        volumes=tuple(volumes),
        ports_to_publish=tuple(ports_to_publish),
        publish_host_ip=proxy_host,
    )


def _resolve_token_env(
    plan: SmolmachinesBottlePlan, host_env: dict[str, str],
) -> dict[str, str]:
    """Resolve the egress token env-var values from the host's
    environ so they reach the bundle's process env via docker's
    `-e NAME` inheritance. Empty when no routes declare auth."""
    effective_env = {**host_env, **plan.agent_provision.provisioned_env}
    return egress_resolve_token_values(plan.egress_plan.token_env_map, effective_env)


def _agent_from_path(plan: SmolmachinesBottlePlan) -> Path:
    """Return the `.smolmachine` artifact used for `machine create --from`.

    Prefer a committed VM artifact when one is recorded and still
    present. If the file was removed, fall back to the normal image
    build + pack cache path.
    """
    committed = read_committed_image(plan.slug)
    if committed:
        committed_path = Path(committed)
        if committed_path.is_file():
            info(f"using committed smolmachine {str(committed_path)!r}")
            return committed_path

    # Build the agent image and pack it into a `.smolmachine`
    # artifact (or hit the per-Dockerfile-digest cache). Runs here,
    # not in prepare, so the docker-build output doesn't garble the
    # dashboard's preflight modal.
    return _ensure_smolmachine(
        plan.agent_image,
        dockerfile=plan.agent_dockerfile_path,
    )


def _ensure_smolmachine(image_ref: str, *, dockerfile: str = "") -> Path:
    """Build the agent docker image and convert it into a
    `.smolmachine` artifact, caching the result under
    `~/.cache/bot-bottle/smolmachines/` keyed by the docker image
    ID (so a Dockerfile change automatically invalidates the cache).

    Returns the `.smolmachine.smolmachine` sidecar path — that's
    the file `machine create --from` consumes (pack create produces
    a launcher binary at `.smolmachine` plus the sidecar alongside
    it; the sidecar is the actual artifact).

    Conversion path: `docker build` (the existing layer cache
    makes no-change rebuilds cheap) → `docker save` to a tarball
    → spin up an ephemeral registry on a private docker network →
    `crane push --insecure` from a one-shot container on the same
    network → `smolvm pack create --image localhost:<host port>/...`
    → tear down the registry + network. The crane push detour
    sidesteps the Docker-Desktop daemon's HTTPS preference for
    non-loopback registries — see the `local_registry` module
    docstring for the gory details.

    Each pack-create costs several seconds even on a hot cache,
    so we skip the whole pipeline when the cached sidecar is
    already on disk for this image ID."""
    _SMOLMACHINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    docker_mod.build_image(image_ref, _REPO_DIR, dockerfile=dockerfile)
    # `sha256:abcd...` -> `abcd...` first 16 chars: short enough to
    # keep filenames manageable, long enough to make collisions
    # astronomically unlikely.
    digest = docker_mod.image_id(image_ref).split(":", 1)[-1][:16]
    binary = _SMOLMACHINE_CACHE_DIR / f"{digest}.smolmachine"
    sidecar = _SMOLMACHINE_CACHE_DIR / f"{digest}.smolmachine.smolmachine"
    if sidecar.is_file():
        return sidecar
    tarball = _SMOLMACHINE_CACHE_DIR / f"{digest}.image.tar"
    docker_mod.save(image_ref, str(tarball))
    # On Linux, `docker save -o` writes the tarball with owner-only
    # permissions (mode 600). The crane push container runs as UID
    # 65532 (distroless nonroot) and can't read it through a bind
    # mount unless world-read is set. The tarball is temporary and
    # lives in ~/.cache, so 644 is safe.
    tarball.chmod(0o644)
    try:
        with ephemeral_registry() as handle:
            push_ref = f"{handle.push_endpoint}/bot-bottle:{digest}"
            pack_ref = f"{handle.pull_endpoint}/bot-bottle:{digest}"
            crane_push_tarball(handle, str(tarball), push_ref)
            _smolvm.pack_create(pack_ref, binary)
    finally:
        # Tarball is ~500MB-1GB for the agent image; reclaim once
        # the smolmachine artifact exists. The artifact itself is
        # the long-lived cache entry.
        tarball.unlink(missing_ok=True)
    return sidecar
