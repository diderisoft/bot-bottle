"""Docker's primitives for reattaching running agent containers to the gateway
(PRD 0081); the flow itself is `backend.gateway_attach`.

Each bottle is a compose project whose `agent` service holds a pinned address
on the shared gateway network, which is how the orchestrator knows it.
"""

from __future__ import annotations

from ...gateway import GATEWAY_NETWORK
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..gateway_attach import GatewayAttachError, GatewayAttacher, RunningBottle
from ..util import AGENT_CA_PATH
from .compose import compose_project_name, list_active_slugs
from .util import run_docker


class DockerGatewayAttacher(GatewayAttacher):
    def __init__(self, network: str = GATEWAY_NETWORK) -> None:
        self._network = network

    def running_bottles(self) -> list[RunningBottle]:
        bottles: list[RunningBottle] = []
        for slug in list_active_slugs(raise_on_error=True, warn_on_error=False):
            container = self._agent_container(slug)
            if not container:
                continue  # project up but its agent isn't running (yet)
            proc = run_docker([
                "docker", "inspect", "--format",
                f'{{{{with index .NetworkSettings.Networks "{self._network}"}}}}'
                "{{.IPAddress}}{{end}}",
                container,
            ])
            if proc.returncode != 0:
                raise GatewayAttachError(
                    f"could not inspect {slug}'s agent container: "
                    f"{proc.stderr.strip() or '<no stderr>'}"
                )
            source_ip = proc.stdout.strip()
            if source_ip:
                bottles.append(RunningBottle(ref=container, slug=slug, source_ip=source_ip))
        return bottles

    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        self._exec(
            bottle, ["-u", "0", "-i"],
            ["sh", "-c",
             f"mkdir -p /usr/local/share/ca-certificates && cat > {AGENT_CA_PATH} && "
             f"chmod 644 {AGENT_CA_PATH} && update-ca-certificates >/dev/null"],
            stdin=ca_pem, what="installing the gateway CA",
        )

    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        # `printenv` exits 1 when the variable is unset: no secret, not an error.
        proc = run_docker(["docker", "exec", bottle.ref, "printenv", ENV_VAR_SECRET_NAME])
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _agent_container(self, slug: str) -> str:
        proc = run_docker([
            "docker", "ps", "--quiet",
            "--filter", f"label=com.docker.compose.project={compose_project_name(slug)}",
            "--filter", "label=com.docker.compose.service=agent",
        ])
        if proc.returncode != 0:
            raise GatewayAttachError(
                f"could not list {slug}'s containers: {proc.stderr.strip() or '<no stderr>'}"
            )
        return proc.stdout.strip().split("\n", 1)[0]

    @staticmethod
    def _exec(
        bottle: RunningBottle, flags: list[str], argv: list[str], *, stdin: str, what: str,
    ) -> None:
        proc = run_docker(["docker", "exec", *flags, bottle.ref, *argv], input=stdin)
        if proc.returncode != 0:
            raise GatewayAttachError(
                f"{what} failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or '<no stderr>'}"
            )


__all__ = ["DockerGatewayAttacher"]
