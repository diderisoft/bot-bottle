"""Docker's primitives for reattaching running agent containers to the gateway;
the flow itself is `backend.gateway_attach`.

Each bottle is a compose project whose `agent` service holds a pinned address
on the shared gateway network, which is how the orchestrator knows it. The
gateway is pinned too, so its address in the agent's proxy env stays valid
across gateway restarts; reattach only confirms it. The CA
install runs as root inside the agent through an absolute `/bin/sh` with a
pinned system PATH, so nothing the agent planted on its own PATH runs as root;
the secret is read from the container's config on the host side, without
running agent code at all.
"""

from __future__ import annotations

import json
import subprocess
from urllib.parse import urlsplit

from ...control.gateway import GATEWAY_NETWORK
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..gateway_attach import GatewayAttachError, GatewayAttacher, RunningBottle
from ..util import install_agent_ca_script
from .compose import compose_project_name, list_active_slugs
from .util import run_docker

_EXEC_TIMEOUT_SECONDS = 60


class DockerGatewayAttacher(GatewayAttacher):
    def __init__(self, network: str = GATEWAY_NETWORK) -> None:
        self._network = network

    def running_bottles(self) -> list[RunningBottle]:
        bottles: list[RunningBottle] = []
        for slug in list_active_slugs(raise_on_error=True, warn_on_error=False):
            container = self._agent_container(slug)
            if not container:
                continue  # project up but its agent container isn't running
            source_ip = self._docker(
                ["inspect", "--format",
                 f'{{{{with index .NetworkSettings.Networks "{self._network}"}}}}'
                 "{{.IPAddress}}{{end}}", container],
                what=f"inspecting {slug}'s agent container",
            ).strip()
            bottles.append(RunningBottle(ref=container, slug=slug, source_ip=source_ip))
        return bottles

    def point_at_gateway(self, bottle: RunningBottle, address: str) -> None:
        # The proxy URL is baked into the agent's environ, which can't be
        # rewritten; the pinned gateway address keeps it valid, so confirm it.
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
