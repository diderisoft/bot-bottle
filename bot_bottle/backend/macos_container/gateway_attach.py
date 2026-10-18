"""macOS's primitives for reattaching running agent containers to the gateway
(PRD 0081); the flow itself is `backend.gateway_attach`.

Agents are `container` instances named by slug, attributed by their address on
the shared agent network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..gateway_attach import GatewayAttachError, GatewayAttacher, RunningBottle
from ..util import AGENT_CA_PATH
from . import util as container_mod
from .enumerate import CONTAINER_NAME_PREFIX, EnumerationError, enumerate_active

_STAGED_CA = "/tmp/bot-bottle-gateway-ca.pem"


class MacosGatewayAttacher(GatewayAttacher):
    def __init__(self, network: str) -> None:
        self._network = network

    def running_bottles(self) -> list[RunningBottle]:
        bottles: list[RunningBottle] = []
        try:
            agents = enumerate_active()
        except EnumerationError as e:
            raise GatewayAttachError(f"could not list running macOS bottles: {e}") from e
        for agent in agents:
            name = f"{CONTAINER_NAME_PREFIX}{agent.slug}"
            source_ip = container_mod.inspect_container_network_ip(name, self._network)
            if source_ip is None:
                raise GatewayAttachError(f"could not inspect container {name}")
            if source_ip:
                bottles.append(RunningBottle(ref=name, slug=agent.slug, source_ip=source_ip))
        return bottles

    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        # `container exec` has no stdin we can rely on, so stage the PEM with
        # `container cp`, then install it as root.
        with tempfile.TemporaryDirectory(prefix="bb-ca-") as tmp:
            pem = Path(tmp) / "gateway-ca.pem"
            pem.write_text(ca_pem)
            self._run(["container", "cp", str(pem), f"{bottle.ref}:{_STAGED_CA}"],
                      what="copying the gateway CA in")
        self._run(
            ["container", "exec", "--user", "root", bottle.ref, "sh", "-c",
             f"mkdir -p /usr/local/share/ca-certificates && "
             f"mv {_STAGED_CA} {AGENT_CA_PATH} && chmod 644 {AGENT_CA_PATH} && "
             "update-ca-certificates >/dev/null"],
            what="installing the gateway CA",
        )

    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        return container_mod.read_container_env(bottle.ref, ENV_VAR_SECRET_NAME)

    @staticmethod
    def _run(argv: list[str], *, what: str) -> None:
        result = container_mod.run_container_argv(argv)
        if result.returncode != 0:
            raise GatewayAttachError(
                f"{what} failed (exit {result.returncode}): "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )


__all__ = ["MacosGatewayAttacher"]
