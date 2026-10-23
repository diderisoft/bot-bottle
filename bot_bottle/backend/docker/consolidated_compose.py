"""Agent-only compose for the consolidated docker backend.

One shared gateway serves every bottle, so this renders just the agent,
attached to the **external shared gateway network** with the
pinned source IP the orchestrator allocated, and pointed at the gateway's
address for egress (and, around the proxy, for git-http / supervise).

Pure: it takes the launch-time `LaunchContext` values (gateway address,
source IP, network) and the prepared plan, and returns a compose dict — no
docker, so it's testable in isolation.
"""

from __future__ import annotations

from typing import Any

from ...egress import Egress
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from .bottle_plan import DockerBottlePlan


def consolidated_agent_compose(
    plan: DockerBottlePlan,
    *,
    gateway_ip: str,
    source_ip: str,
    network: str,
) -> dict[str, Any]:
    """A compose spec with only the agent service, on the external gateway
    network at `source_ip`, proxying egress through `gateway_ip`."""
    # Deliver the identity token as egress proxy credentials — the gateway
    # reads Proxy-Authorization, validates the (source_ip, token) pair, and
    # strips it before upstream. git-http/supervise get it via their own
    # headers (git config extraHeader / MCP header).
    token = getattr(plan, "identity_token", "")
    cred = f"bottle:{token}@" if token else ""
    proxy_url = f"http://{cred}{gateway_ip}:{plan.settings.infra.egress_port}"
    # git-http + supervise live on the gateway too and must NOT go through the
    # egress proxy — the agent reaches them directly by the gateway address.
    no_proxy = f"localhost,127.0.0.1,{gateway_ip}"
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
    for name, value in sorted(plan.agent_provision.guest_env.items()):
        env.append(f"{name}={value}")
    # Forwarded vars: bare name → inherits from the compose-up process env so
    # the secret value never lands on argv or in the compose file.
    for name in sorted(plan.forwarded_env.keys()):
        env.append(name)
    # ENV_VAR_SECRET: bare name so the value comes from the compose subprocess
    # env (set in launch.py) and is never written to the compose file on disk.
    if getattr(plan, "env_var_secret", ""):
        env.append(ENV_VAR_SECRET_NAME)
    env.extend(Egress().agent_env_entries(plan.egress_plan))

    service: dict[str, Any] = {
        "image": plan.image,
        "container_name": plan.container_name,
        "command": ["sleep", "infinity"],
        # Pinned address on the shared gateway network — the orchestrator
        # registered this IP, and the gateway attributes the bottle by it.
        "networks": {network: {"ipv4_address": source_ip}},
        "environment": env,
        # No raw sockets: with NET_RAW a root process could forge addresses on
        # the shared gateway network and impersonate another bottle (the
        # macOS backend drops it for the same reason).
        "cap_drop": ["NET_RAW"],
    }
    if plan.use_runsc:
        service["runtime"] = "runsc"

    return {
        "name": f"bot-bottle-{plan.slug}",
        "services": {"agent": service},
        # The gateway network is created + owned by the orchestrator; compose
        # attaches to it (external) and must not create or destroy it.
        "networks": {network: {"external": True}},
    }


__all__ = ["consolidated_agent_compose"]
