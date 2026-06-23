"""Launch flow for the macOS Apple Container backend.

This backend keeps the explicit proxy-env enforcement model for v1:
the agent container is attached only to a host-only Apple Container
network, while the sidecar bundle is attached to a NAT network first
and the host-only network second. The sidecar's host-only IP is
discovered from `container inspect` and stamped into the agent's
HTTP_PROXY / HTTPS_PROXY env vars.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Generator

from ...bottle_state import egress_state_dir, git_gate_state_dir
from ...egress import EGRESS_ROUTES_IN_CONTAINER, egress_resolve_token_values
from ...git_gate import revoke_git_gate_provisioned_keys
from ...log import die, info, warn
from ...supervise import QUEUE_DIR_IN_CONTAINER, SUPERVISE_PORT
from ...util import expand_tilde
from ..docker.egress import EGRESS_CA_IN_CONTAINER, EGRESS_PORT
from ..docker.git_gate import (
    GIT_GATE_ACCESS_HOOK_IN_CONTAINER,
    GIT_GATE_CREDS_DIR_IN_CONTAINER,
    GIT_GATE_ENTRYPOINT_IN_CONTAINER,
    GIT_GATE_HOOK_IN_CONTAINER,
)
from ..docker.sidecar_bundle import (
    SIDECAR_BUNDLE_DOCKERFILE,
    SIDECAR_BUNDLE_IMAGE,
)
from ..docker.egress import egress_tls_init
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from . import util as container_mod
from .bottle import MacosContainerBottle
from .bottle_plan import MacosContainerBottlePlan


_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)
_AGENT_SLEEP_SECONDS = "2147483647"
_GIT_HTTP_PORT = 9420
_GIT_GATE_READY_FILE = "/run/git-gate/ready"


def internal_network_name(slug: str) -> str:
    return f"bot-bottle-net-{slug}"


def egress_network_name(slug: str) -> str:
    return f"bot-bottle-egress-{slug}"


def sidecar_container_name(slug: str) -> str:
    return f"bot-bottle-sidecars-{slug}"


@contextmanager
def launch(
    plan: MacosContainerBottlePlan,
    *,
    provision: Callable[[MacosContainerBottlePlan, "MacosContainerBottle"], str | None],
) -> Generator[MacosContainerBottle, None, None]:
    """Build, run, provision, and yield an Apple Container bottle."""
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
        plan = _mint_certs(plan)
        _build_images(plan)

        internal_network = internal_network_name(plan.slug)
        egress_network = egress_network_name(plan.slug)
        _create_networks(internal_network, egress_network, stack)

        sidecar_name = sidecar_container_name(plan.slug)
        container_mod.force_remove_container(sidecar_name)
        _start_sidecar_bundle(plan, sidecar_name, internal_network, egress_network)
        stack.callback(container_mod.force_remove_container, sidecar_name)
        _stage_git_gate(plan, sidecar_name)

        sidecar_ip = container_mod.container_ipv4_on_network(
            sidecar_name, internal_network,
        )
        plan = _stamp_agent_urls(plan, sidecar_ip)

        container_mod.force_remove_container(plan.container_name)
        _start_agent(plan, internal_network, sidecar_ip)
        stack.callback(container_mod.force_remove_container, plan.container_name)

        bottle = MacosContainerBottle(
            plan.container_name,
            teardown,
            None,
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
        teardown()


def _mint_certs(plan: MacosContainerBottlePlan) -> MacosContainerBottlePlan:
    egress_ca_host, egress_ca_cert_only = egress_tls_init(
        egress_state_dir(plan.slug),
    )
    egress_plan = dataclasses.replace(
        plan.egress_plan,
        mitmproxy_ca_host_path=egress_ca_host,
        mitmproxy_ca_cert_only_host_path=egress_ca_cert_only,
    )
    return dataclasses.replace(plan, egress_plan=egress_plan)


def _build_images(plan: MacosContainerBottlePlan) -> None:
    container_mod.build_image(
        SIDECAR_BUNDLE_IMAGE,
        _REPO_DIR,
        dockerfile=SIDECAR_BUNDLE_DOCKERFILE,
    )
    container_mod.build_image(
        plan.image,
        _REPO_DIR,
        dockerfile=plan.dockerfile_path,
    )


def _create_networks(
    internal_network: str,
    egress_network: str,
    stack: ExitStack,
) -> None:
    container_mod.create_network(internal_network, internal=True)
    stack.callback(container_mod.remove_network, internal_network)
    container_mod.create_network(egress_network)
    stack.callback(container_mod.remove_network, egress_network)


def _start_sidecar_bundle(
    plan: MacosContainerBottlePlan,
    sidecar_name: str,
    internal_network: str,
    egress_network: str,
) -> None:
    argv = _sidecar_run_argv(plan, sidecar_name, internal_network, egress_network)
    effective_env = {**dict(os.environ), **plan.agent_provision.provisioned_env}
    token_values = egress_resolve_token_values(
        plan.egress_plan.token_env_map, effective_env,
    )
    env = {**os.environ, **token_values}
    info(f"container run sidecar bundle {sidecar_name}")
    result = subprocess.run(
        argv, capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        die(
            f"container run for sidecar bundle {sidecar_name} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def _start_agent(
    plan: MacosContainerBottlePlan,
    internal_network: str,
    sidecar_ip: str,
) -> None:
    argv = _agent_run_argv(plan, internal_network, sidecar_ip)
    env = {
        **os.environ,
        **plan.forwarded_env,
    }
    info(f"container run agent {plan.container_name}")
    result = subprocess.run(
        argv, capture_output=True, text=True, env=env, check=False,
    )
    if result.returncode != 0:
        die(
            f"container run for agent {plan.container_name} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def _stamp_agent_urls(
    plan: MacosContainerBottlePlan,
    sidecar_ip: str,
) -> MacosContainerBottlePlan:
    proxy_url = f"http://{sidecar_ip}:{EGRESS_PORT}"
    supervise_url = ""
    if plan.supervise_plan is not None:
        supervise_url = f"http://{sidecar_ip}:{SUPERVISE_PORT}/"
    git_gate_url = ""
    if plan.git_gate_plan.upstreams:
        git_gate_url = f"http://{sidecar_ip}:{_GIT_HTTP_PORT}"
    return dataclasses.replace(
        plan,
        agent_proxy_url=proxy_url,
        agent_git_gate_url=git_gate_url,
        agent_supervise_url=supervise_url,
    )


def _stage_git_gate(plan: MacosContainerBottlePlan, sidecar_name: str) -> None:
    gp = plan.git_gate_plan
    if not gp.upstreams:
        return

    container_mod.exec_container(
        sidecar_name,
        [
            "mkdir",
            "-p",
            str(Path(GIT_GATE_HOOK_IN_CONTAINER).parent),
            GIT_GATE_CREDS_DIR_IN_CONTAINER,
            "/git",
            str(Path(_GIT_GATE_READY_FILE).parent),
        ],
    )

    for host_path, container_path in _git_gate_files(plan):
        container_mod.copy_into_container(
            sidecar_name, host_path, container_path,
        )

    container_mod.exec_container(
        sidecar_name,
        [
            "sh",
            "-c",
            "chmod 755 "
            f"{GIT_GATE_ENTRYPOINT_IN_CONTAINER} "
            f"{GIT_GATE_HOOK_IN_CONTAINER} "
            f"{GIT_GATE_ACCESS_HOOK_IN_CONTAINER} && "
            f"chmod 600 {GIT_GATE_CREDS_DIR_IN_CONTAINER}/* && "
            f"touch {_GIT_GATE_READY_FILE}",
        ],
    )


def _git_gate_files(
    plan: MacosContainerBottlePlan,
) -> tuple[tuple[str, str], ...]:
    gp = plan.git_gate_plan
    files: list[tuple[str, str]] = [
        (str(gp.entrypoint_script), GIT_GATE_ENTRYPOINT_IN_CONTAINER),
        (str(gp.hook_script), GIT_GATE_HOOK_IN_CONTAINER),
        (str(gp.access_hook_script), GIT_GATE_ACCESS_HOOK_IN_CONTAINER),
    ]
    for upstream in gp.upstreams:
        files.append((
            expand_tilde(upstream.identity_file),
            f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{upstream.name}-key",
        ))
        if upstream.known_hosts_file:
            files.append((
                str(upstream.known_hosts_file),
                f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{upstream.name}-known_hosts",
            ))
    return tuple(files)


def _sidecar_run_argv(
    plan: MacosContainerBottlePlan,
    sidecar_name: str,
    internal_network: str,
    egress_network: str,
) -> list[str]:
    argv = [
        "container", "run",
        "--name", sidecar_name,
        "--detach",
        "--rm",
        "--network", egress_network,
        "--network", internal_network,
        "--dns", _sidecar_dns(),
        "--env", f"BOT_BOTTLE_SIDECAR_DAEMONS={','.join(_sidecar_daemons(plan))}",
    ]
    for entry in _sidecar_env_entries(plan):
        argv += ["--env", entry]
    for host_path, container_path, read_only in _sidecar_mounts(plan):
        argv += ["--mount", _mount_spec(host_path, container_path, read_only)]
    argv.append(SIDECAR_BUNDLE_IMAGE)
    return argv


def _agent_run_argv(
    plan: MacosContainerBottlePlan,
    internal_network: str,
    sidecar_ip: str,
) -> list[str]:
    argv = [
        "container", "run",
        "--name", plan.container_name,
        "--detach",
        "--rm",
        "--network", internal_network,
    ]
    for entry in _agent_env_entries(plan, sidecar_ip):
        argv += ["--env", entry]
    argv += [plan.image, "sleep", _AGENT_SLEEP_SECONDS]
    return argv


def _sidecar_dns() -> str:
    return container_mod.dns_server()


def _sidecar_daemons(plan: MacosContainerBottlePlan) -> tuple[str, ...]:
    daemons = ["egress"]
    if plan.git_gate_plan.upstreams:
        daemons += ["git-gate", "git-http"]
    if plan.supervise_plan is not None:
        daemons.append("supervise")
    return tuple(daemons)


def _sidecar_env_entries(plan: MacosContainerBottlePlan) -> tuple[str, ...]:
    env: list[str] = []
    if plan.egress_plan.routes:
        env.extend(sorted(plan.egress_plan.token_env_map.keys()))
    if plan.git_gate_plan.upstreams:
        env.append(f"BOT_BOTTLE_GIT_GATE_READY_FILE={_GIT_GATE_READY_FILE}")
    if plan.supervise_plan is not None:
        env += [
            f"SUPERVISE_BOTTLE_SLUG={plan.slug}",
            f"SUPERVISE_QUEUE_DIR={QUEUE_DIR_IN_CONTAINER}",
            f"SUPERVISE_PORT={SUPERVISE_PORT}",
        ]
    return tuple(env)


def _sidecar_mounts(
    plan: MacosContainerBottlePlan,
) -> tuple[tuple[str, str, bool], ...]:
    mounts: list[tuple[str, str, bool]] = []

    ep = plan.egress_plan
    mounts.append((
        str(ep.mitmproxy_ca_host_path.parent),
        str(Path(EGRESS_CA_IN_CONTAINER).parent),
        False,
    ))
    if ep.routes:
        mounts.append((
            str(ep.routes_path.parent),
            str(Path(EGRESS_ROUTES_IN_CONTAINER).parent),
            True,
        ))

    sp = plan.supervise_plan
    if sp is not None:
        mounts.append((str(sp.queue_dir), QUEUE_DIR_IN_CONTAINER, False))

    return tuple(mounts)

def _mount_spec(host_path: str, container_path: str, read_only: bool) -> str:
    spec = f"type=bind,source={host_path},target={container_path}"
    if read_only:
        spec += ",readonly"
    return spec


def _agent_env_entries(
    plan: MacosContainerBottlePlan,
    sidecar_ip: str,
) -> tuple[str, ...]:
    proxy_url = f"http://{sidecar_ip}:{EGRESS_PORT}"
    no_proxy = _agent_no_proxy(plan, sidecar_ip)
    env = [
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
    if plan.agent_git_gate_url:
        env.append(f"GIT_GATE_URL={plan.agent_git_gate_url}")
    if plan.agent_supervise_url:
        env.append(f"MCP_SUPERVISE_URL={plan.agent_supervise_url}")
    for name, value in sorted(plan.agent_provision.guest_env.items()):
        env.append(f"{name}={value}")
    for name in sorted(plan.forwarded_env.keys()):
        env.append(name)
    return tuple(env)


def _agent_no_proxy(plan: MacosContainerBottlePlan, sidecar_ip: str) -> str:
    hosts = ["localhost", "127.0.0.1", sidecar_ip]
    return ",".join(hosts)
