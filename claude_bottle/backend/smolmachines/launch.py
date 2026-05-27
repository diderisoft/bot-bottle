"""End-to-end launch flow for the smolmachines backend
(PRD 0023 chunks 2d + 4b).

Brings up the per-bottle docker bridge + sidecar bundle (with
real daemons + their config files), creates + starts the smolvm
guest pointed at the bundle's pinned IP via TSI's
`--allow-cidr <bundle-ip>/32` allowlist, yields a
`SmolmachinesBottle` handle, tears everything down on context
exit.

The bundle's daemons consume the inner Plans the docker backend
already produces: pipelock reads its yaml + CA from the
PipelockProxyPlan; egress reads routes + CAs from the EgressPlan
+ EGRESS_UPSTREAM_PROXY pointing at `127.0.0.1:8888` (bundle
local), since the agent dials pipelock first (not egress) on the
smolmachines path. Git-gate + supervise plumb through the same
plans the docker backend uses, minus the docker-network fields
that don't apply here."""

from __future__ import annotations

import dataclasses
import os
import time
from contextlib import ExitStack, contextmanager
from typing import Callable, Generator

from ...egress import EGRESS_ROUTES_IN_CONTAINER, egress_resolve_token_values
from ...pipelock import (
    PIPELOCK_CA_CERT_IN_CONTAINER,
    PIPELOCK_CA_KEY_IN_CONTAINER,
)
from ...supervise import QUEUE_DIR_IN_CONTAINER, SUPERVISE_PORT
from ...util import expand_tilde
from ..docker.egress import (
    EGRESS_CA_IN_CONTAINER,
    EGRESS_PIPELOCK_CA_IN_CONTAINER,
    EGRESS_PORT as _EGRESS_PORT,
    egress_tls_init,
)
from ..docker.git_gate import (
    GIT_GATE_ACCESS_HOOK_IN_CONTAINER,
    GIT_GATE_CREDS_DIR_IN_CONTAINER,
    GIT_GATE_ENTRYPOINT_IN_CONTAINER,
    GIT_GATE_HOOK_IN_CONTAINER,
    GIT_GATE_PORT as _GIT_GATE_PORT,
)
from ..docker.pipelock import (
    BUNDLE_LOCAL_PIPELOCK_URL,
    PIPELOCK_PORT as _PIPELOCK_PORT_STR,
    pipelock_tls_init,
)
from . import sidecar_bundle as _bundle
from . import smolvm as _smolvm
from .bottle import SmolmachinesBottle
from .bottle_plan import SmolmachinesBottlePlan


# Container-internal listening ports for each bundle daemon. The
# bundle publishes each one on a random host loopback port (see
# `_bundle.start_bundle`), and `_bundle.bundle_host_port` looks
# them up post-start. Pipelock's port is an env-overridable string
# in docker.pipelock; coerce to int here.
_PIPELOCK_PORT = int(_PIPELOCK_PORT_STR)
_SUPERVISE_PORT = SUPERVISE_PORT


@contextmanager
def launch(
    plan: SmolmachinesBottlePlan,
    *,
    provision: Callable[[SmolmachinesBottlePlan, str], str | None],
) -> Generator[SmolmachinesBottle, None, None]:
    """Build + run the bottle and yield a handle; tear everything
    down on exit. Errors during bringup unwind any partial state
    via the ExitStack."""
    stack = ExitStack()
    try:
        # 1. Per-bottle docker bridge.
        network = _bundle.bundle_network_name(plan.slug)
        _bundle.create_bundle_network(network, plan.bundle_subnet, plan.bundle_gateway)
        stack.callback(_bundle.remove_bundle_network, network)

        # 2. Mint per-bottle CAs and update the inner Plans with
        # their launch-time paths. pipelock always runs in the
        # bundle; egress's CA is only minted when the bottle
        # declares routes (otherwise egress runs idle without
        # MITM and the CA files would be unused).
        ca_cert_host, ca_key_host = pipelock_tls_init(plan.proxy_plan.yaml_path.parent)
        proxy_plan = dataclasses.replace(
            plan.proxy_plan,
            ca_cert_host_path=ca_cert_host,
            ca_key_host_path=ca_key_host,
        )
        egress_plan = plan.egress_plan
        if egress_plan.routes:
            egress_ca_host, egress_ca_cert_only = egress_tls_init(
                plan.egress_plan.routes_path.parent,
            )
            egress_plan = dataclasses.replace(
                egress_plan,
                mitmproxy_ca_host_path=egress_ca_host,
                mitmproxy_ca_cert_only_host_path=egress_ca_cert_only,
                pipelock_ca_host_path=ca_cert_host,
                # On smolmachines, egress's upstream is pipelock
                # on the bundle's localhost — they're in the same
                # container's network namespace.
                pipelock_proxy_url=BUNDLE_LOCAL_PIPELOCK_URL,
            )
        plan = dataclasses.replace(
            plan, proxy_plan=proxy_plan, egress_plan=egress_plan,
        )

        # 3. Build the BundleLaunchSpec from the (now-resolved)
        # inner Plans: daemon subset, env, bind-mounts. The spec's
        # ports_to_publish list expands depending on which daemons
        # the agent needs to reach from the smolvm guest.
        bundle_spec = _bundle_launch_spec(plan, network)
        token_env = _resolve_token_env(plan, os.environ)
        _bundle.start_bundle(bundle_spec, env={**os.environ, **token_env})
        stack.callback(_bundle.stop_bundle, plan.slug)

        # 4. Discover the host-side ports docker assigned for the
        # bundle's published container ports, and bind the
        # agent's URLs to `127.0.0.1:<host port>`. Docker container
        # IPs (192.168.x.x in the daemon's bridge) aren't
        # reachable from the smolvm guest on macOS — TSI uses
        # macOS networking, and macOS sees the daemon's bridge
        # via the published-port loopback forward only.
        #
        # Proxy hop order matches the docker backend: when the
        # bottle declares egress routes, the agent's first hop is
        # egress (for token injection), then pipelock. Without
        # routes, the agent dials pipelock directly. Whichever
        # one is "agent-facing" is the daemon whose port we
        # publish on host loopback; the other stays bundle-
        # internal as the upstream proxy.
        if plan.egress_plan.routes:
            agent_facing_port = _EGRESS_PORT
        else:
            agent_facing_port = _PIPELOCK_PORT
        agent_facing_host_port = _bundle.bundle_host_port(
            plan.slug, agent_facing_port,
        )
        agent_proxy_url = f"http://127.0.0.1:{agent_facing_host_port}"
        agent_git_gate_host = ""
        if plan.git_gate_plan.upstreams:
            git_gate_host_port = _bundle.bundle_host_port(
                plan.slug, _GIT_GATE_PORT,
            )
            agent_git_gate_host = f"127.0.0.1:{git_gate_host_port}"
        agent_supervise_url = ""
        if plan.supervise_plan is not None:
            supervise_host_port = _bundle.bundle_host_port(
                plan.slug, _SUPERVISE_PORT,
            )
            agent_supervise_url = f"http://127.0.0.1:{supervise_host_port}/"

        # Stamp the URLs onto the plan + guest_env. provision_git
        # and provision_supervise read the plan fields; the agent
        # reads guest_env on every exec_claude.
        guest_env = {
            **plan.guest_env,
            "HTTPS_PROXY": agent_proxy_url,
            "HTTP_PROXY":  agent_proxy_url,
        }
        if agent_git_gate_host:
            guest_env["GIT_GATE_URL"] = f"git://{agent_git_gate_host}"
        if agent_supervise_url:
            guest_env["MCP_SUPERVISE_URL"] = agent_supervise_url
        plan = dataclasses.replace(
            plan,
            guest_env=guest_env,
            agent_proxy_url=agent_proxy_url,
            agent_git_gate_host=agent_git_gate_host,
            agent_supervise_url=agent_supervise_url,
        )

        # 5. smolvm VM. --from carries the pre-packed .smolmachine
        # artifact (built by prepare); --allow-cidr + -e carry the
        # per-bottle TSI allowlist + env. The allowlist is
        # `127.0.0.1/32` because every bundle daemon the agent
        # reaches is fronted by a host loopback port-forward.
        # Smolfile isn't usable here — smolvm 0.8.0 makes `--from`
        # and `--smolfile` mutually exclusive.
        _smolvm.machine_create(
            plan.machine_name,
            from_path=plan.agent_from_path,
            allow_cidrs=["127.0.0.1/32"],
            env=plan.guest_env,
        )
        stack.callback(_smolvm.machine_delete, plan.machine_name)
        _smolvm.machine_start(plan.machine_name)
        stack.callback(_smolvm.machine_stop, plan.machine_name)

        # 6. Reclaim /home/node for the node user. smolvm's pack
        # process remaps OCI-layer ownership to the host invoker's
        # uid (501 on macOS) rather than preserving the image's
        # uid 1000 — so without this chown, node can't write its
        # own dotfiles (claude appendFileSync on
        # ~/.claude.json bails with ENOENT/EPERM and the TUI hangs
        # without surfacing the error).
        _smolvm.machine_exec(
            plan.machine_name,
            ["chown", "-R", "node:node", "/home/node"],
        )

        # Wait briefly for the VM to settle. Back-to-back smolvm
        # machine_exec calls immediately after machine_start
        # occasionally SIGKILL the in-VM child at ~100ms (looks
        # like a VM warm-up race in libkrun's exec channel).
        # 1.5s is empirically enough to dodge it; provisioning
        # already takes seconds so the wait is amortized.
        time.sleep(1.5)

        # 7. Provision (CA / prompt / skills / git / supervise).
        prompt_path = provision(plan, plan.machine_name)

        yield SmolmachinesBottle(
            plan.machine_name,
            prompt_path=prompt_path,
            guest_env=plan.guest_env,
        )
    finally:
        stack.close()


def _bundle_launch_spec(
    plan: SmolmachinesBottlePlan, network: str
) -> _bundle.BundleLaunchSpec:
    """Build a BundleLaunchSpec from the resolved inner Plans.

    Daemons in the CSV:
      - egress + pipelock are always present (pipelock is the
        agent's first hop; egress is its upstream).
      - git-gate is conditional on plan.git_gate_plan.upstreams.
      - supervise is conditional on plan.supervise_plan.

    Env + volumes are the union of the four daemons' needs, with
    daemon-private values only (HTTPS_PROXY is scoped to the
    egress process by egress_entrypoint.sh — see PRD 0024's bundle
    bind-address PR)."""
    daemons: list[str] = ["egress", "pipelock"]
    env: list[str] = []
    volumes: list[tuple[str, str, bool]] = []

    # In this Docker-Desktop-compatible topology, whichever daemon
    # is "agent-facing" gets its port published on the host
    # loopback (see `_ensure_smolmachine`'s discovery loop) and the
    # other stays bundle-internal. The bundle is NOT reachable by
    # bridge IP from the smolvm guest, so the
    # PRD-0023-chunk-3 EGRESS_LISTEN_HOST=127.0.0.1 mitigation
    # isn't needed: the agent can only dial whatever daemon's
    # host port we publish, period.

    # --- pipelock ---------------------------------------------
    pp = plan.proxy_plan
    volumes += [
        (str(pp.yaml_path), "/etc/pipelock.yaml", True),
        (str(pp.ca_cert_host_path), PIPELOCK_CA_CERT_IN_CONTAINER, True),
        (str(pp.ca_key_host_path), PIPELOCK_CA_KEY_IN_CONTAINER, True),
    ]

    # --- egress -----------------------------------------------
    ep = plan.egress_plan
    if ep.routes:
        env.append(f"EGRESS_UPSTREAM_PROXY={ep.pipelock_proxy_url}")
        env.append(f"EGRESS_UPSTREAM_CA={EGRESS_PIPELOCK_CA_IN_CONTAINER}")
        volumes += [
            (str(ep.routes_path), EGRESS_ROUTES_IN_CONTAINER, True),
            (str(ep.mitmproxy_ca_host_path), EGRESS_CA_IN_CONTAINER, True),
            (str(ep.pipelock_ca_host_path), EGRESS_PIPELOCK_CA_IN_CONTAINER, True),
        ]
        # Bare-name entries for upstream-token slots. Their values
        # come from the docker-run subprocess env (inherited from
        # the operator's shell), never landing on argv.
        for token_env in sorted(ep.token_env_map.keys()):
            env.append(token_env)

    # --- git-gate ---------------------------------------------
    extra_hosts: list[str] = []
    gp = plan.git_gate_plan
    if gp.upstreams:
        daemons.append("git-gate")
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

    # --- supervise --------------------------------------------
    sp = plan.supervise_plan
    if sp is not None:
        daemons.append("supervise")
        env += [
            f"SUPERVISE_BOTTLE_SLUG={plan.slug}",
            f"SUPERVISE_QUEUE_DIR={QUEUE_DIR_IN_CONTAINER}",
            f"SUPERVISE_PORT={SUPERVISE_PORT}",
        ]
        volumes.append((str(sp.queue_dir), QUEUE_DIR_IN_CONTAINER, False))

    # Container ports the agent reaches from the smolvm guest —
    # published on host loopback so the guest can dial via TSI +
    # macOS networking. The HTTP/HTTPS chokepoint is whichever
    # daemon's port we publish: egress when routes are declared
    # (token injection first, then forwards to bundle-internal
    # pipelock), pipelock otherwise.
    if ep.routes:
        ports_to_publish: list[int] = [_EGRESS_PORT]
    else:
        ports_to_publish = [_PIPELOCK_PORT]
    if gp.upstreams:
        ports_to_publish.append(_GIT_GATE_PORT)
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
    )


def _resolve_token_env(
    plan: SmolmachinesBottlePlan, host_env: object
) -> dict[str, str]:
    """Resolve the egress token env-var values from the host's
    environ so they reach the bundle's process env via docker's
    `-e NAME` inheritance. Empty when no routes declare auth."""
    ep = plan.egress_plan
    if not ep.routes:
        return {}
    return egress_resolve_token_values(ep.token_env_map, dict(host_env))
