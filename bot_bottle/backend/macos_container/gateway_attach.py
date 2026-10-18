"""macOS's primitives for reattaching running agent containers to the gateway
(PRD 0081); the flow itself is `backend.gateway_attach`.

Agents are `container` instances named by slug, attributed by their address on
the shared agent network. The CA is staged under root-only `/root` (not
world-writable `/tmp`, where the agent could plant a symlink) and installed
through an absolute `/bin/sh` with a pinned system PATH; the secret is read
from the container's config on the host side.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..gateway_attach import GatewayAttachError, GatewayAttacher, RunningBottle
from ..util import install_agent_ca_script
from . import util as container_mod
from .enumerate import CONTAINER_NAME_PREFIX, EnumerationError, enumerate_active

_STAGED_CA = "/root/bot-bottle-gateway-ca.pem"
_EXEC_TIMEOUT_SECONDS = 60


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
            ["container", "exec", "--user", "root", bottle.ref,
             "/bin/sh", "-c", install_agent_ca_script(_STAGED_CA)],
            what="installing the gateway CA",
        )

    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        data = json.loads(self._run(["container", "inspect", bottle.ref],
                                    what="reading the agent container's config") or "[]")
        record = data[0] if isinstance(data, list) and data else {}
        config = record.get("configuration", {}) if isinstance(record, dict) else {}
        init = config.get("initProcess", {}) if isinstance(config, dict) else {}
        env = init.get("environment", []) if isinstance(init, dict) else []
        prefix = f"{ENV_VAR_SECRET_NAME}="
        return next((e[len(prefix):] for e in env
                     if isinstance(e, str) and e.startswith(prefix)), "")

    @staticmethod
    def _run(argv: list[str], *, what: str) -> str:
        try:
            result = container_mod.run_container_argv(argv, timeout=_EXEC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as e:
            raise GatewayAttachError(f"{what} timed out after {e.timeout:.0f}s") from e
        if result.returncode != 0:
            detail = "".join(c for c in (result.stderr or "").strip() if c.isprintable())
            raise GatewayAttachError(
                f"{what} failed (exit {result.returncode}): {detail or '<no stderr>'}")
        return result.stdout


__all__ = ["MacosGatewayAttacher"]
