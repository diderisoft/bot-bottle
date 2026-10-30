"""macOS launch: the agent joins the shared host-only gateway network and
proxies through the per-host gateway.

Apple has no `--ip`, so the agent runs before it is registered: gateway up, run
agent, read its DHCP address, register, provision. The identity token therefore
reaches the agent only at `container exec` time
(`bottle.MacosContainerBottle`); the init is a bare `sleep`, so every real
command carries it.

The agent runs with `--cap-drop CAP_NET_RAW`. Apple grants NET_RAW by default,
which would let it forge a neighbour's source address; NET_ADMIN is already
absent. Together they make the source address a reliable attribution key,
independent of the token.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import shlex
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Generator

from ...contract.hostctl import Settings
from ...bottle_state import read_committed_image
from ...egress import Egress
from ...image_cache import check_stale
from ...lib.log import die, info
from ... import resources
from .. import BottleImages, isolation_probe, oci_agent_image
from ..launch_common import (
    LaunchTeardown,
    registration_tokens,
    gateway_agent_urls,
    provision_git_gate_keys,
    stage_gateway_ca,
    terminal_title,
)
from ..base import home_volume_name
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from . import util as container_mod
from . import host_firewall
from .bottle import MacosContainerBottle
from .gateway_hosts import GATEWAY_HOSTNAME, set_gateway_host
from . import nested_containers as nested_containers_mod
from .bottle_plan import MacosContainerBottlePlan
from ...orchestrator.store.config_store import resolve_teardown_timeout
from ...paths import host_db_path
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME, new_env_var_secret
from .infra import MacosInfraService
from .consolidated_launch import (
    GatewayEndpoint,
    ensure_gateway,
    register_agent,
    deprovision_consolidated,
)

if TYPE_CHECKING:
    from ..adoption import AdoptionRecord

_AGENT_SLEEP_SECONDS = "2147483647"


def build_or_load_images(plan: MacosContainerBottlePlan) -> BottleImages:
    """The agent image ref. The gateway image belongs to `ensure_gateway`."""
    return BottleImages(agent=_layer_nested_containers(plan, _agent_image(plan)))


def _build_image_fn(settings: Settings) -> Callable[..., None]:
    """`container_mod.build_image` bound to the settings' cache and DNS
    options."""
    return functools.partial(
        container_mod.build_image, no_cache=settings.operation.no_cache,
        dns_override=settings.infra.macos_container_dns)


def _container_runtime(settings: Settings) -> oci_agent_image.OciRuntime:
    # Apple pulls with `container image pull`. No smoke test, as for local
    # builds.
    return oci_agent_image.OciRuntime(
        pull_argv=("container", "image", "pull"),
        image_exists=container_mod.image_exists,
        tag_image=container_mod.tag_image,
        build_image=_build_image_fn(settings),
    )


def _agent_image(plan: MacosContainerBottlePlan) -> str:
    committed = read_committed_image(plan.slug)
    if committed and container_mod.image_exists(committed):
        info(f"using committed image {committed!r}")
        return committed
    # Prefer the published image (or an overlay on it); the nested-containers
    # layer goes on top either way. None means build the bottle's Dockerfile.
    published = oci_agent_image.resolve_published_agent_image(
        plan, _container_runtime(plan.settings), harness=plan.harness,
        image=plan.image, smoke_test=(),
    )
    if published is not None:
        return published
    if plan.spec.image_policy == "prefer-cached" and container_mod.image_exists(plan.image):
        info(f"using cached agent image {plan.image!r}")
        return plan.image
    if plan.spec.image_policy == "cached":
        if not container_mod.image_exists(plan.image):
            die(
                f"cached agent image {plan.image!r} not found; "
                "run without --cached-images to build it"
            )
        info(f"using cached agent image {plan.image!r}")
        return plan.image
    _build_image_fn(plan.settings)(
        plan.image, str(resources.build_root()), dockerfile=plan.dockerfile_path)
    return plan.image


def _layer_nested_containers(
    plan: MacosContainerBottlePlan, agent_image: str,
) -> str:
    """Add the nested-container tooling as a separate derived tag, so other
    bottles carry none of it.
    """
    if not plan.nested_containers:
        return agent_image
    pinned_base = container_mod.pinned_local_image_ref(agent_image)
    derived = f"{pinned_base}{nested_containers_mod.IMAGE_SUFFIX}"
    if plan.spec.image_policy == "prefer-cached" and container_mod.image_exists(derived):
        info(f"using cached nested-container image {derived!r}")
        return derived
    if plan.spec.image_policy == "cached":
        if not container_mod.image_exists(derived):
            die(
                f"cached nested-container image {derived!r} not found; "
                "run without --cached-images to build it"
            )
        info(f"using cached nested-container image {derived!r}")
        return derived
    return nested_containers_mod.build_image(
        pinned_base,
        _build_image_fn(plan.settings),
    )


@contextmanager
def launch(
    plan: MacosContainerBottlePlan,
    images: BottleImages,
    *,
    provision: Callable[[MacosContainerBottlePlan, "MacosContainerBottle"], str | None],
) -> Generator[MacosContainerBottle, None, None]:
    """Run, register, provision and yield an Apple Container bottle."""
    plan = dataclasses.replace(
        plan,
        agent_provision=dataclasses.replace(plan.agent_provision, image=str(images.agent)),
    )
    teardown = LaunchTeardown(plan, "macos-container")
    stack = teardown.stack

    try:
        # Infra first: the agent's proxy env needs the gateway at run time. A
        # cold bring-up re-points running bottles.
        service = MacosInfraService(plan.settings)
        endpoint = ensure_gateway(service=service)

        # Mint deploy keys, then point at the gateway's CA and ports.
        plan = _provision_git_gate_keys(plan)
        plan = _install_gateway_ca(plan, endpoint)
        plan = _stamp_agent_urls(plan, endpoint)
        plan = dataclasses.replace(plan, env_var_secret=new_env_var_secret())

        # Run the agent on its home volume. It has no token yet.
        container_mod.force_remove_container(plan.container_name)
        home_kept = _ensure_home(plan)
        _start_agent(plan, endpoint)
        stack.callback(container_mod.force_remove_container, plan.container_name)

        # Resolve the gateway name before anything execs, then read the DHCP
        # address (polling: `run --detach` can return first) and register by
        # it.
        set_gateway_host(plan.container_name, endpoint.gateway_ip)
        source_ip = container_mod.wait_container_ipv4_on_network(
            plan.container_name, endpoint.network,
        )
        if not source_ip:
            die(
                f"agent {plan.container_name} never got an address on "
                f"{endpoint.network}"
            )
        token_values = registration_tokens(plan)
        teardown_timeout = resolve_teardown_timeout(host_db_path())
        ctx = register_agent(
            plan.egress_plan,
            plan.git_gate_plan,
            source_ip=source_ip,
            endpoint=endpoint,
            tokens=token_values,
            env_var_secret=plan.env_var_secret,
        )
        stack.callback(deprovision_consolidated, ctx.bottle_id, service=service,
                       timeout=teardown_timeout)
        info(
            f"agent {plan.container_name} registered "
            f"(gateway {endpoint.gateway_ip}, ip {source_ip})"
        )

        # Put the token on the plan: git-gate's extraHeader and the supervise
        # MCP header bypass the proxy, so they need it too.
        plan = dataclasses.replace(plan, identity_token=ctx.identity_token)

        exec_env = {
            **_identity_proxy_env(endpoint, ctx.identity_token, plan.settings.infra.egress_port),
            **nested_containers_mod.guest_env(plan.nested_containers),
        }
        bottle = MacosContainerBottle(
            plan.container_name,
            teardown,
            None,
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            harness=plan.harness,
            terminal_title=terminal_title(plan.spec),
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
            exec_env=exec_env,
        )
        bottle.home_kept = home_kept
        # Prove the only way out is the gateway, then provision.
        # IPv6 on the agent bridge reaches the host where pf can't name it
        # (#720); the host firewall's daemon turns it off, within a minute of
        # the network being (re)created.
        agent_subnet = host_firewall.agent_subnet()
        if not host_firewall.wait_ipv6_off(agent_subnet):
            die("ISOLATION FAILURE: IPv6 is still on on the agent network's bridge, "
                "so agents can reach this Mac over it. Refusing to run the agent "
                "(fail-closed). Install or update the host firewall with "
                "`bot-bottle install --backend=macos-container`.")
        isolation_probe.verify_isolation(
            lambda script: bottle.exec(script).stdout,
            what=f"agent container {plan.container_name}",
            remedy=("The host firewall keeps agents off this Mac; install or "
                    "update it with `bot-bottle install "
                    "--backend=macos-container`."),
            check_dns=True,
            # The host's own address on the agent network: the agents' default
            # route and resolver, which a host-only network still reaches.
            host_ips=(host_firewall.host_address(agent_subnet),),
        )
        bottle.prompt_path = provision(plan, bottle)

        if plan.nested_containers:
            nested_containers_mod.prepare_guest_devices(
                plan.container_name, container_mod.exec_container_as_root,
            )
            nested_containers_mod.start(bottle)

        bottle.registry_id = ctx.bottle_id
        bottle.env_var_secret = getattr(ctx, "env_var_secret", "")
        yield bottle
    finally:
        teardown()



@contextmanager
def adopted(
    bottle_id: str, record: "AdoptionRecord", settings: Settings,
) -> Generator[MacosContainerBottle, None, None]:
    """A bottle a previous host controller launched: exec into it, then tear it
    down as its holder would have (remove the container, then deregister)."""
    try:
        yield attach(bottle_id, record)
    finally:
        container_mod.force_remove_container(record.container)
        if record.registry_id:
            deprovision_consolidated(record.registry_id, service=MacosInfraService(settings),
                                     timeout=resolve_teardown_timeout(host_db_path()))


def attach(bottle_id: str, record: "AdoptionRecord") -> MacosContainerBottle:
    """A bottle a previous host controller launched, to exec into."""
    del bottle_id
    return MacosContainerBottle(record.container, lambda: None, None)


def stale_checks(plan: MacosContainerBottlePlan) -> None:
    """Raise StaleImageError if a cached image is past the threshold. Only
    under the 'cached' policy."""
    if plan.spec.image_policy not in ("cached", "prefer-cached"):
        return
    committed = read_committed_image(plan.slug)
    if committed and container_mod.image_exists(committed):
        ts = container_mod.image_created_at(committed)
        if ts is not None:
            check_stale(f"agent image {committed!r}", ts)
        return
    if container_mod.image_exists(plan.image):
        ts = container_mod.image_created_at(plan.image)
        if ts is not None:
            check_stale(f"agent image {plan.image!r}", ts)


def _provision_git_gate_keys(
    plan: MacosContainerBottlePlan,
) -> MacosContainerBottlePlan:
    return dataclasses.replace(plan, git_gate_plan=provision_git_gate_keys(plan))


def _install_gateway_ca(
    plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint,
) -> MacosContainerBottlePlan:
    """Stage the gateway's CA for the provisioner."""
    return dataclasses.replace(
        plan, egress_plan=stage_gateway_ca(plan, endpoint.gateway_ca_pem))


def _stamp_agent_urls(
    plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint,
) -> MacosContainerBottlePlan:
    """Point git-gate and supervise at the gateway by `GATEWAY_HOSTNAME`, never
    by IP: these URLs are baked in at provision time, and the name resolves
    through the rewritable `/etc/hosts`. Both bypass the proxy."""
    del endpoint  # addressed by name, via /etc/hosts
    git_gate_url, supervise_url = gateway_agent_urls(plan, GATEWAY_HOSTNAME)
    return dataclasses.replace(
        plan,
        agent_git_gate_url=git_gate_url,
        agent_supervise_url=supervise_url,
    )


def _proxy_url(egress_port: int, identity_token: str = "") -> str:
    """The agent's proxy URL with the identity token as credentials. Uses the
    gateway's name, since process env can't change if the gateway moves."""
    cred = f"bottle:{identity_token}@" if identity_token else ""
    return f"http://{cred}{GATEWAY_HOSTNAME}:{egress_port}"


def _no_proxy() -> str:
    # git-http and supervise bypass the proxy. Address-free, like the proxy
    # URL.
    return f"localhost,127.0.0.1,{GATEWAY_HOSTNAME}"


def _identity_proxy_env(
    endpoint: GatewayEndpoint, identity_token: str, egress_port: int,
) -> dict[str, str]:
    """The token-bearing proxy env applied at `container exec`, and the only
    source of `*_PROXY`. `exec --env` appends rather than replaces, so nothing
    may be set at run time. See `_agent_env_entries`."""
    if not identity_token:
        return {}
    del endpoint  # the gateway is named, not addressed
    url = _proxy_url(egress_port, identity_token)
    return {
        "HTTPS_PROXY": url, "HTTP_PROXY": url,
        "https_proxy": url, "http_proxy": url,
    }


def _ensure_home(plan: MacosContainerBottlePlan) -> bool:
    """The bottled agent's home volume (PRD 0110): made on its first launch and
    seeded from the image's home, since an Apple volume starts empty. Whether
    it was already there, from an earlier run."""
    name = home_volume_name(plan.slug)
    if container_mod.volume_exists(name):
        info(f"using {plan.slug}'s home from its last run ({name})")
        return True
    made = subprocess.run(["container", "volume", "create", name],
                          capture_output=True, text=True, check=False)
    if made.returncode != 0:
        die(f"container volume create {name} failed: "
            f"{(made.stderr or '').strip() or '<no stderr>'}")
    home = shlex.quote(plan.guest_home)
    # The image's home, with its owner and mode on the volume's root; ext4's
    # lost+found isn't the agent's.
    seed = (f"set -e; cp -a {home}/. /bb-seed/; rm -rf /bb-seed/lost+found; "
            f'chown "$(stat -c %u:%g {home})" /bb-seed; chmod "$(stat -c %a {home})" /bb-seed')
    seeded = subprocess.run(
        ["container", "run", "--rm", "--name", f"{plan.container_name}-home",
         "--user", "root", "--entrypoint", "sh", "--volume", f"{name}:/bb-seed",
         plan.image, "-c", seed],
        capture_output=True, text=True, check=False)
    if seeded.returncode != 0:
        container_mod.remove_volume(name)
        die(f"seeding {name} from {plan.image} failed: "
            f"{(seeded.stderr or '').strip() or '<no stderr>'}")
    info(f"made {plan.slug}'s home ({name}) from {plan.image}")
    return False


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
        # Without NET_RAW the agent can't spoof neighbours' addresses. See the
        # module docstring.
        "--cap-drop", "CAP_NET_RAW",
        # Its home, kept across stop and resume (`_ensure_home`).
        "--volume", f"{home_volume_name(plan.slug)}:{plan.guest_home}",
    ]
    for entry in _agent_env_entries(plan, endpoint):
        argv += ["--env", entry]
    # No-op init; every command arrives via exec, with the token.
    argv += [plan.image, "sleep", _AGENT_SLEEP_SECONDS]
    return argv


def _agent_env_entries(
    plan: MacosContainerBottlePlan, endpoint: GatewayEndpoint,
) -> tuple[str, ...]:
    # No `*_PROXY` at run time: `exec --env` appends, so a token-less entry
    # here would sit first in `environ`, and first-wins runtimes (Rust) would
    # proxy without the token. A process egressing before exec still fails
    # closed: the agent network is host-only.
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
    if getattr(plan, "env_var_secret", ""):
        env.append(f"{ENV_VAR_SECRET_NAME}={plan.env_var_secret}")
    for name, value in sorted(plan.agent_provision.guest_env.items()):
        env.append(f"{name}={value}")
    # Bare names: values come from the `container run` process env.
    for name in sorted(plan.forwarded_env.keys()):
        env.append(name)
    env.extend(Egress().agent_env_entries(plan.egress_plan))
    return tuple(env)


__all__ = ["launch"]
