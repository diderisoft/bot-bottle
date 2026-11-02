"""Agent-only compose spec: the agent on its own external agent network at
its pinned source IP, proxied through the gateway. Pure.
"""

from __future__ import annotations

from typing import Any

from ...environment import resource_prefix
from ...egress import Egress
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..bottle_size import BOTTLE_MEMORY_MIB
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from .bottle_plan import DockerBottlePlan

from ..base import home_volume_name

def consolidated_agent_compose(
    plan: DockerBottlePlan,
    *,
    gateway_ip: str,
    source_ip: str,
    network: str,
) -> dict[str, Any]:
    """A compose spec with just the agent, on `network` at `source_ip`. Its
    proxy env is the exec's (`agent_exec_env`), not the container's."""
    del gateway_ip  # in the exec env, which a kept container's next run renews
    env: list[str] = [
        f"NODE_EXTRA_CA_CERTS={AGENT_CA_PATH}",
        f"SSL_CERT_FILE={AGENT_CA_BUNDLE}",
        f"REQUESTS_CA_BUNDLE={AGENT_CA_BUNDLE}",
    ]
    for name, value in sorted(plan.agent_provision.guest_env.items()):
        env.append(f"{name}={value}")
    # Bare names: values come from the compose-up process env.
    for name in sorted(plan.forwarded_env.keys()):
        env.append(name)
    # Bare name; the value is set in the compose subprocess env.
    if getattr(plan, "env_var_secret", ""):
        env.append(ENV_VAR_SECRET_NAME)
    env.extend(Egress().agent_env_entries(plan.egress_plan))

    service: dict[str, Any] = {
        "image": plan.image,
        "container_name": plan.container_name,
        "command": ["sleep", "infinity"],
        # The registered address the gateway attributes this bottle by.
        "networks": {network: {"ipv4_address": source_ip}},
        "environment": env,
        # Without NET_RAW, root can't forge addresses to impersonate another
        # bottle.
        "cap_drop": ["NET_RAW"],
        "mem_limit": f"{BOTTLE_MEMORY_MIB}m",
        # Its home, kept across stop and resume. Docker fills an empty named
        # volume from the image's home on first use.
        "volumes": [f"{home_volume_name(plan.slug)}:{plan.guest_home}"],
    }
    if plan.use_runsc:
        service["runtime"] = "runsc"

    return {
        "name": f"{resource_prefix()}{plan.slug}",
        "services": {"agent": service},
        # Owned by the launcher (`agent_network`); compose attaches but
        # never creates or removes it.
        "networks": {network: {"external": True}},
        # Likewise the home (`launch._ensure_home`): `compose down --volumes`
        # leaves an external volume alone.
        "volumes": {home_volume_name(plan.slug): {"external": True}},
    }


def agent_exec_env(plan: DockerBottlePlan, *, gateway_ip: str, identity_token: str,
                   ) -> dict[str, str]:
    """The env every `docker exec` into the agent gets: its proxy, the identity
    token as credentials (the gateway checks (source_ip, token) and strips it),
    and what bypasses it. Never the container's own env: the token and the
    gateway's address are this run's, and a container kept for the next run
    (`preserve_container`) keeps its env."""
    cred = f"bottle:{identity_token}@" if identity_token else ""
    proxy_url = f"http://{cred}{gateway_ip}:{plan.settings.infra.egress_port}"
    # git-http and supervise are reached directly, not through the proxy.
    no_proxy = f"localhost,127.0.0.1,{gateway_ip}"
    return {
        "HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url,
        "https_proxy": proxy_url, "http_proxy": proxy_url,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
    }


__all__ = ["agent_exec_env", "consolidated_agent_compose"]
