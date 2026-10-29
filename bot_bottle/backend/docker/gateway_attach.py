"""Docker primitives for reattaching running agents (the flow is in
`backend.gateway_attach`).

Each agent is on its own agent network with the gateway at a pinned address
there. A recreated gateway has lost those attachments, so reattach joins it to
each bottle's network at that address again, then confirms the proxy address.
A bottle launched before per-agent networks is still on the shared gateway
network, where the gateway's address is fixed too.

The CA install runs as root through `/bin/sh` with a pinned PATH; the secret
is read from the container config on the host, without running agent code.
"""

from __future__ import annotations

import json
import subprocess
from urllib.parse import urlsplit

from ...control.gateway import GATEWAY_NAME, GATEWAY_NETWORK
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..gateway_attach import GatewayAttachError, GatewayAttacher, RunningBottle
from ..util import install_agent_ca_script
from .agent_network import (
    AgentNetworkError, agent_network_name, connect_gateway, network_subnet,
)
from .compose import compose_project_name, list_active_slugs
from .gateway_net import agent_subnet_addresses
from .util import run_docker

_EXEC_TIMEOUT_SECONDS = 60


class DockerGatewayAttacher(GatewayAttacher):
    def __init__(self, network: str = GATEWAY_NETWORK, gateway: str = GATEWAY_NAME) -> None:
        self._network = network
        self._gateway = gateway

    def running_bottles(self) -> list[RunningBottle]:
        bottles: list[RunningBottle] = []
        for slug in list_active_slugs(raise_on_error=True, warn_on_error=False):
            container = self._agent_container(slug)
            if not container:
                continue  # project up, agent container not running
            networks = self._networks(container, what=f"inspecting {slug}'s agent container")
            source_ip = networks.get(agent_network_name(slug)) or networks.get(self._network, "")
            bottles.append(RunningBottle(ref=container, slug=slug, source_ip=source_ip))
        return bottles

    def point_at_gateway(self, bottle: RunningBottle, address: str) -> None:
        network = agent_network_name(bottle.slug)
        if network in self._networks(bottle.ref, what="reading the agent's networks"):
            # Its own network: put the gateway back at its pinned address there.
            try:
                address, _ = agent_subnet_addresses(network_subnet(network))
                connect_gateway(network, self._gateway, address)
            except AgentNetworkError as e:
                raise GatewayAttachError(str(e)) from e
        # The proxy URL is in the agent's environ and can't change; confirm it.
        proxy = self._env_value(bottle, "HTTPS_PROXY")
        target = urlsplit(proxy).hostname if proxy else None
        if target != address:
            raise GatewayAttachError(
                f"it proxies through {target or 'nothing'} but the gateway is at "
                f"{address}; relaunch it")

    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        self._docker(
            ["exec", "-u", "0", "-i", bottle.ref, "/bin/sh", "-c", install_agent_ca_script("-")],
            stdin=ca_pem, what="installing the gateway CA",
        )

    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        return self._env_value(bottle, ENV_VAR_SECRET_NAME)

    def _networks(self, container: str, *, what: str) -> dict[str, str]:
        """The container's networks, each with its address there."""
        out = self._docker(
            ["inspect", "--format",
             "{{range $name, $net := .NetworkSettings.Networks}}"
             "{{$name}}={{$net.IPAddress}} {{end}}", container],
            what=what,
        )
        networks: dict[str, str] = {}
        for entry in out.split():
            name, _, ip = entry.partition("=")
            networks[name] = ip
        return networks

    def _env_value(self, bottle: RunningBottle, name: str) -> str:
        env = json.loads(self._docker(
            ["inspect", "--format", "{{json .Config.Env}}", bottle.ref],
            what="reading the agent container's environment",
        ) or "null") or []
        prefix = f"{name}="
        return next((e[len(prefix):] for e in env if e.startswith(prefix)), "")

    def _agent_container(self, slug: str) -> str:
        out = self._docker(
            ["ps", "--quiet",
             "--filter", f"label=com.docker.compose.project={compose_project_name(slug)}",
             "--filter", "label=com.docker.compose.service=agent"],
            what=f"listing {slug}'s containers",
        )
        return out.strip().split("\n", 1)[0]

    @staticmethod
    def _docker(args: list[str], *, what: str, stdin: str | None = None) -> str:
        try:
            proc = run_docker(["docker", *args], input=stdin, timeout=_EXEC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as e:
            raise GatewayAttachError(f"{what} timed out after {e.timeout:.0f}s") from e
        if proc.returncode != 0:
            detail = "".join(c for c in proc.stderr.strip() if c.isprintable())
            raise GatewayAttachError(
                f"{what} failed (exit {proc.returncode}): {detail or '<no stderr>'}")
        return proc.stdout


__all__ = ["DockerGatewayAttacher"]
