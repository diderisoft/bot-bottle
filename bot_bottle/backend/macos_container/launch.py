"""Launch flow for the macOS Apple Container backend (PRD 0070).

The agent container attaches to the **shared host-only gateway network** and
proxies egress through the one per-host gateway, replacing the per-bottle
companion container removed in #385.

The order differs from docker's, forced by Apple Container 1.0.0 having no
`--ip` (see `consolidated_launch`): the agent is started *before* it is
registered, because its DHCP-assigned address — the attribution key — does not
exist until then.

    gateway up -> run agent -> read its IP -> register it -> provision

Two things follow from that inversion:

- The **identity token** is minted by registration and so cannot be in the
  agent's run-time env; it rides the proxy URL applied at `container exec`
  time (`bottle.MacosContainerBottle`). `/resolve` requires it (#366), so
  egress without it is denied — hence the bare `sleep` init: every real agent
  command goes through exec and therefore carries the token.
- The agent is run with `--cap-drop CAP_NET_RAW`. Apple Container grants
  NET_RAW by default, which would let an agent open a raw socket and forge a
  neighbour's source address on the shared segment. NET_ADMIN is already
  absent (the agent cannot change its own address or route), so dropping
  NET_RAW is what closes the source-address half of PRD 0070's invariant:
  "a packet's source address, as seen by the orchestrator, provably identifies
  the originating bottle." The identity token is the other half — an attacker
  would need to forge the address *and* steal the token — but the invariant is
  a stated precondition of consolidation, so it is enforced on its own terms
  rather than left to the token.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Generator

from ...bottle_state import (
    egress_state_dir,
    git_gate_state_dir,
    read_committed_image,
)
from ...egress import (
    egress_agent_env_entries,
    egress_resolve_token_values,
)
from ...git_gate import (
    provision_git_gate_dynamic_keys,
    revoke_git_gate_provisioned_keys,
)
from ...git_http_backend import DEFAULT_PORT as _GIT_HTTP_PORT
from ...log import die, info, warn
from ...supervise import SUPERVISE_PORT
from ..docker.egress import EGRESS_PORT
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from . import util as container_mod
from .bottle import MacosContainerBottle
from .gateway_hosts import (
    GATEWAY_HOSTNAME,
    refresh_gateway_host,
    set_gateway_host,
)
from .bottle_plan import MacosContainerBottlePlan
from ...orchestrator.config_store import resolve_teardown_timeout
from .consolidated_launch import (
    GatewayEndpoint,
    ensure_gateway,
    register_agent,
    teardown_consolidated,
)

_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)
_AGENT_SLEEP_SECONDS = "2147483647"


@contextmanager
def launch(
    plan: MacosContainerBottlePlan,
    *,
    provision: Callable[[MacosContainerBottlePlan, "MacosContainerBottle"], str | None],
) -> Generator[MacosContainerBottle, None, None]:
    """Build, run, register, provision, and yield an Apple Container bottle on
    the shared per-host gateway."""
    stack = ExitStack()
    bottle_for_revoke = plan.manifest.bottle
    git_gate_dir_for_revoke = git_gate_state_dir(plan.slug)

    def teardown() -> None:
        teardown_exc: BaseException | None = None
        try:
            stack.close()
        except BaseException as exc:  # noqa: W0718 - teardown must continue
            teardown_exc = exc
            warn(f"macos-container teardown failed: {exc!r}")
        revoke_git_gate_provisioned_keys(bottle_for_revoke, git_gate_dir_for_revoke)
        if teardown_exc is not None:
            raise teardown_exc

    try:
        plan = _build_images(plan)

        # Step 1: the per-host singletons. Must precede the agent run — its
        # proxy env needs the gateway's address at `container run` time.
        endpoint = ensure_gateway()
        # The gateway's address may have changed since these bottles launched
        # (any infra recreate re-runs DHCP). They name the gateway rather than
        # address it, so re-pointing /etc/hosts re-attaches them in place
        # instead of leaving them stranded until relaunch.
        refresh_gateway_host(endpoint.gateway_ip)

        # Step 2: mint this bottle's deploy keys, then point it at the SHARED
        # gateway's CA + git-http/supervise ports.
        plan = _provision_git_gate_keys(plan)
        plan = _install_gateway_ca(plan, endpoint)
        plan = _stamp_agent_urls(plan, endpoint)

        # Step 3: run the agent. It has no identity token yet — registration
        # needs the address this run assigns.
        container_mod.force_remove_container(plan.container_name)
        _start_agent(plan, endpoint)
        stack.callback(container_mod.force_remove_container, plan.container_name)

        # Step 4: read the assigned address and register by it. This is the
        # attribution key; `--cap-drop CAP_NET_RAW` at run is what makes it
        # unforgeable. Poll: `container run --detach` can return before vmnet's
        # DHCP has assigned the address.
        # Resolve the gateway name before anything execs: every agent-facing
        # URL uses it, so the entry must exist for the first connection.
        set_gateway_host(plan.container_name, endpoint.gateway_ip)
        source_ip = container_mod.wait_container_ipv4_on_network(
            plan.container_name, endpoint.network,
        )
        if not source_ip:
            die(
                f"agent {plan.container_name} never got an address on "
                f"{endpoint.network}"
            )
        effective_env = {**os.environ, **plan.agent_provision.provisioned_env}
        token_values = egress_resolve_token_values(
            plan.egress_plan.token_env_map, effective_env,
        )
        ctx = register_agent(
            plan.egress_plan,
            plan.git_gate_plan,
            source_ip=source_ip,
            endpoint=endpoint,
            image_ref=plan.image,
            tokens=token_values,
        )
        stack.callback(
            teardown_consolidated, ctx.bottle_id,
            orchestrator_url=ctx.orchestrator_url,
            timeout=resolve_teardown_timeout(),
        )
        info(
            f"agent {plan.container_name} registered "
            f"(gateway {endpoint.gateway_ip}, ip {source_ip})"
        )

        # Stamp the token onto the plan so provision-time consumers can read it,
        # not only the exec-time egress proxy. git-gate's gitconfig extraHeader
        # and the supervise MCP --header both reach the gateway on NO_PROXY (they
        # bypass the egress proxy that carries the token), so without this the
        # gateway's /resolve fail-closes and every git fetch/push and supervise
        # call from the bottle is denied. Registration already produced the
        # token above, so — unlike the run-time env — the plan CAN carry it.
        plan = dataclasses.replace(plan, identity_token=ctx.identity_token)

        bottle = MacosContainerBottle(
            plan.container_name,
            teardown,
            None,
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            agent_provider_template=plan.agent_provider_template,
            terminal_title=(
                f"{plan.spec.label} ({plan.spec.agent_name})"
                if plan.spec.label else plan.spec.agent_name
            ),
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
            exec_env=_identity_proxy_env(endpoint, ctx.identity_token),
        )
        bottle.prompt_path = provision(plan, bottle)

        yield bottle
    finally:
        teardown()


def _build_images(plan: MacosContainerBottlePlan) -> MacosContainerBottlePlan:
    """Build the agent image. The gateway's own image is built by
    `ensure_gateway` — it belongs to the shared singleton, not to a bottle."""
    committed = read_committed_image(plan.slug)
    if committed and container_mod.image_exists(committed):
        info(f"using committed image {committed!r}")
        return dataclasses.replace(
            plan,
            agent_provision=dataclasses.replace(
                plan.agent_provision, image=committed,
            ),
        )
    container_mod.build_image(
        plan.image, _REPO_DIR, dockerfile=plan.dockerfile_path,
    )
    return plan


def _provision_git_gate_keys(
    plan: MacosContainerBottlePlan,
) -> MacosContainerBottlePlan:
    if not plan.git_gate_plan.upstreams:
        return plan
    git_gate_plan = provision_git_gate_dynamic_keys(
        plan.manifest.bottle,
        plan.git_gate_plan,
        git_gate_state_dir(plan.slug),
    )
    return dataclasses.replace(plan, git_gate_plan=git_gate_plan)


def _install_gateway_ca(
    plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint,
) -> MacosContainerBottlePlan:
    """Stage the SHARED gateway's CA for the provisioner to install, replacing
    the per-bottle CA the companion container used to mint. Every bottle on
    this host trusts this one CA."""
    ca_dir = egress_state_dir(plan.slug) / "gateway-ca"
    ca_dir.mkdir(parents=True, exist_ok=True)
    ca_file = ca_dir / "gateway-ca.pem"
    ca_file.write_text(endpoint.gateway_ca_pem)
    egress_plan = dataclasses.replace(
        plan.egress_plan,
        mitmproxy_ca_host_path=ca_file,
        mitmproxy_ca_cert_only_host_path=ca_file,
    )
    return dataclasses.replace(plan, egress_plan=egress_plan)


def _stamp_agent_urls(
    plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint,
) -> MacosContainerBottlePlan:
    """Point the agent's git-gate insteadOf rewrites + supervise MCP at the
    shared gateway's ports. Both bypass the egress proxy (NO_PROXY covers the
    gateway name).

    Addressed by `GATEWAY_HOSTNAME`, never by IP: these URLs are baked into
    the agent's gitconfig and MCP config at provision time, so an address here
    would strand the bottle the moment the gateway moved. The name is resolved
    per connection through `/etc/hosts`, which stays rewritable while the
    bottle runs."""
    del endpoint  # addressed by name; the address reaches the bottle via /etc/hosts
    git_gate_url = (
        f"http://{GATEWAY_HOSTNAME}:{_GIT_HTTP_PORT}"
        if plan.git_gate_plan.upstreams else ""
    )
    supervise_url = (
        f"http://{GATEWAY_HOSTNAME}:{SUPERVISE_PORT}/"
        if plan.supervise_plan is not None else ""
    )
    return dataclasses.replace(
        plan,
        agent_git_gate_url=git_gate_url,
        agent_supervise_url=supervise_url,
    )


def _proxy_url(identity_token: str = "") -> str:
    """The agent's egress proxy URL. The identity token rides as proxy
    credentials — the gateway reads Proxy-Authorization, resolves the
    (source_ip, token) pair against the control plane, and strips it before
    upstream. Without a valid pair `/resolve` denies the request (#366).

    Names the gateway rather than addressing it: this URL reaches the agent as
    process environment, which cannot be rewritten once the agent is running,
    so an address baked here is unfixable if the gateway moves."""
    cred = f"bottle:{identity_token}@" if identity_token else ""
    return f"http://{cred}{GATEWAY_HOSTNAME}:{EGRESS_PORT}"


def _no_proxy() -> str:
    # git-http + supervise live on the gateway and must NOT go through the
    # egress proxy — the agent reaches them directly by name. Deliberately
    # address-free: NO_PROXY is baked into the run-time env and is therefore
    # just as unfixable as the proxy URL if the gateway moves.
    return f"localhost,127.0.0.1,{GATEWAY_HOSTNAME}"


def _identity_proxy_env(
    endpoint: GatewayEndpoint, identity_token: str,
) -> dict[str, str]:
    """The token-bearing proxy env applied at `container exec` — the only way
    to get the token in, since it does not exist until after the container
    runs (registration keys on the DHCP-assigned address).

    This is the *sole* source of `*_PROXY` for the agent. It deliberately does
    not rely on overriding a run-time value: `container exec --env` appends
    rather than replaces, so a run-time `HTTPS_PROXY` would survive alongside
    this one and first-wins runtimes would read the wrong entry. See
    `_agent_env_entries`."""
    if not identity_token:
        return {}
    del endpoint  # the gateway is named, not addressed
    url = _proxy_url(identity_token)
    return {
        "HTTPS_PROXY": url, "HTTP_PROXY": url,
        "https_proxy": url, "http_proxy": url,
    }


def _start_agent(plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint) -> None:
    argv = _agent_run_argv(plan, endpoint)
    env = {**os.environ, **plan.forwarded_env}
    info(f"container run agent {plan.container_name}")
    result = subprocess.run(
        argv, capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        die(
            f"container run for agent {plan.container_name} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def _agent_run_argv(
    plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint,
) -> list[str]:
    argv = [
        "container", "run",
        "--name", plan.container_name,
        "--detach",
        "--label", "bot-bottle.backend=macos-container",
        "--network", endpoint.network,
        # The attribution invariant: without NET_RAW the agent cannot open a
        # raw socket, so it cannot source-IP-spoof its neighbours on the shared
        # segment. NET_ADMIN is not granted by default, so its address and
        # route are already fixed. See the module docstring.
        "--cap-drop", "CAP_NET_RAW",
    ]
    for entry in _agent_env_entries(plan, endpoint):
        argv += ["--env", entry]
    # The init process is a no-op: every agent command arrives via
    # `container exec`, which is also how the identity token gets in.
    argv += [plan.image, "sleep", _AGENT_SLEEP_SECONDS]
    return argv


def _agent_env_entries(
    plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint,
) -> tuple[str, ...]:
    # No `*_PROXY` here on purpose. The token-bearing URL is applied at
    # `container exec` (`_identity_proxy_env`), and Apple's `container exec
    # --env` **appends** to the run-time environment rather than replacing it:
    # setting a token-less value here leaves two `HTTPS_PROXY` entries in the
    # agent's `environ`, token-less first. Which one a runtime reads is then
    # pure luck — Node takes the last (and worked), Rust's `std::env::var`
    # takes the first, so Codex proxied without its identity token and
    # `/resolve` fail-closed on every request.
    #
    # A token-less proxy URL has no legitimate consumer anyway: the init
    # process is `sleep` and everything that egresses arrives via exec. Its
    # only value was a tidy 403 for unattributed callers, which is not worth
    # silently dropping attribution for. Without it a process that egresses
    # before the exec-time env still fails closed — the agent network is
    # host-only, so there is no route off it except the gateway.
    no_proxy = _no_proxy()
    env = [
        f"NO_PROXY={no_proxy}",
        f"no_proxy={no_proxy}",
        f"NODE_EXTRA_CA_CERTS={AGENT_CA_PATH}",
        f"SSL_CERT_FILE={AGENT_CA_BUNDLE}",
        f"REQUESTS_CA_BUNDLE={AGENT_CA_BUNDLE}",
    ]
    if plan.agent_git_gate_url:
        env.append(f"GIT_GATE_URL={plan.agent_git_gate_url}")
    if plan.agent_supervise_url:
        env.append(f"MCP_SUPERVISE_URL={plan.agent_supervise_url}")
    for name, value in sorted(plan.agent_provision.guest_env.items()):
        env.append(f"{name}={value}")
    # Forwarded vars: bare name → inherits from the `container run` process env
    # so the secret value never lands on argv.
    for name in sorted(plan.forwarded_env.keys()):
        env.append(name)
    env.extend(egress_agent_env_entries(plan.egress_plan))
    return tuple(env)


__all__ = ["launch"]
