"""Agent-only compose spec: the agent on the external shared gateway network at
its pinned source IP, proxied through the gateway. Pure.
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
    """A compose spec with just the agent, on `network` at `source_ip`,
    proxying through `gateway_ip`."""
    # The identity token rides as proxy credentials; the gateway checks
    # (source_ip, token) and strips it.
    token = getattr(plan, "identity_token", "")
    cred = f"bottle:{token}@" if token else ""
    proxy_url = f"http://{cred}{gateway_ip}:{plan.settings.infra.egress_port}"
    # git-http and supervise are reached directly, not through the proxy.
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
    }
    if plan.use_runsc:
        service["runtime"] = "runsc"
    workspace = plan.workspace_plan
    if workspace.mount:
        # The user's directory, read-write.
        service["volumes"] = [{"type": "bind", "source": str(workspace.host_path),
                               "target": workspace.guest_path}]

    return {
        "name": f"bot-bottle-{plan.slug}",
        "services": {"agent": service},
        # Owned by the orchestrator; compose attaches but never creates or
        # removes it.
        "networks": {network: {"external": True}},
    }


__all__ = ["consolidated_agent_compose"]
