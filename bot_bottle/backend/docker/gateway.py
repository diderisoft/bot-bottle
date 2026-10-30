from __future__ import annotations

import ipaddress
import json
import os
import time
from pathlib import Path

from .util import image_exists as docker_image_exists
from .util import daemon_running, run_docker
from .util import tag_image as docker_tag_image
from ...image_registry import RegistryError
from ...infra_image import pinned_infra_artifact, pull_pinned_image
from ...published_artifacts import FORMAT_OCI
from .gateway_transport import DockerGatewayTransport
from ...contract.hostctl import Settings
from ...contract.identity import ORCHESTRATOR_AUTH_JWT_ENV
from ...paths import host_gateway_ca_dir
from ... import resources
from ...control.gateway import (
  Gateway, GatewayTransport, GATEWAY_NAME, GATEWAY_NETWORK,
  GATEWAY_DOCKERFILE, GATEWAY_LABEL, MITMPROXY_HOME,
  DEFAULT_CA_TIMEOUT_SECONDS, CA_POLL_SECONDS, GATEWAY_CA_CERT, GatewayError
)

DEFAULT_GATEWAY_SUBNET = "10.242.255.0/24"
# The gateway's route out; only the gateway is on it.
GATEWAY_EGRESS_NETWORK = "bot-bottle-gateway-egress"
_GATEWAY_SUBNET_LABEL = "bot-bottle.gateway-subnet"
_IP_FORWARD = "net.ipv4.ip_forward"


class DockerGateway(Gateway):
    """The gateway as one fixed-name container. `image_ref` defaults to
    `settings.infra.gateway_image`; `ensure_built` builds it from
    `Dockerfile.gateway`."""

    def __init__(
        self,
        image_ref: str | None = None,
        *,
        settings: Settings,
        name: str = GATEWAY_NAME,
        network: str = GATEWAY_NETWORK,
        control_network: str = "",
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        build_context: Path | None = None,
        dockerfile: str | None = GATEWAY_DOCKERFILE,
        ca_mount_source: str | Path | None = None,
        subnet: str | None = None,
    ) -> None:
        self.image_ref = image_ref or settings.infra.gateway_image
        self.name = name
        self.network = network
        self.egress_network = egress_network
        self._settings = settings
        # When set, the gateway also joins this `--internal` control network to
        # reach the orchestrator by name. Agents are never on it.
        self._control_network = control_network
        # Set by `connect_to_orchestrator`: the control-plane URL and the
        # `gateway` token. Reading the CA or address, and `stop`, work without
        # it.
        self._orchestrator_url = ""
        self._gateway_token = ""
        # Resolved lazily so reading the CA never stages a build root.
        self._build_context = build_context
        self._dockerfile = dockerfile
        self._subnet = (
            subnet or settings.infra.docker_gateway_subnet or DEFAULT_GATEWAY_SUBNET
        )
        self._ca_mount_source = str(
            ca_mount_source or settings.infra.docker_ca_mount or host_gateway_ca_dir()
        )

    def image_exists(self) -> bool:
        return run_docker(["docker", "image", "inspect", self.image_ref]).returncode == 0

    def ensure_built(self) -> None:
        """Build the gateway image, cache-aware: cheap when nothing changed.
        Always builds rather than build-if-missing, since a stale image runs
        old daemons. No-op without a dockerfile. `no_cache` forces a full
        rebuild."""
        if self._dockerfile is None:
            return
        # Pull the release-pinned image when it was built from this install's
        # code; build only when asked (`up --build`), else stop.
        try:
            pin = pinned_infra_artifact(
                "bot-bottle-gateway", FORMAT_OCI,
                local_build=self._settings.infra.infra_build_local)
        except RegistryError as e:
            raise GatewayError(str(e)) from e
        if pin is not None:
            try:
                self.image_ref = pull_pinned_image(
                    pin, local_ref=self.image_ref, image_exists=docker_image_exists,
                    tag_image=docker_tag_image, pull_argv=("docker", "pull"))
            except RegistryError as e:
                raise GatewayError(str(e)) from e
            return
        context = self._build_context or resources.build_root()
        argv = ["docker", "build", "-t", self.image_ref,
                "-f", str(context / self._dockerfile),
                str(context)]
        if self._settings.operation.no_cache:
            argv.insert(2, "--no-cache")
        for name, value in resources.image_build_args(
            self._dockerfile,
            context=context,
        ).items():
            argv[-1:-1] = ["--build-arg", f"{name}={value}"]
        proc = run_docker(argv)
        if proc.returncode != 0:
            raise GatewayError(f"gateway image build failed: {proc.stderr.strip()}")

    def is_running(self) -> bool:
        proc = run_docker([
            "docker", "ps",
            "--filter", f"name=^{self.name}$",
            "--filter", "status=running",
            "--format", "{{.Names}}",
        ])
        return self.name in proc.stdout.split()

    def _running_image_is_current(self) -> bool:
        """True iff the running gateway was created from the current
        `image_ref`; a mismatch means recreate."""
        running = run_docker(["docker", "inspect", "--format", "{{.Image}}", self.name])
        current = run_docker(["docker", "image", "inspect", "--format", "{{.Id}}", self.image_ref])
        if running.returncode != 0 or current.returncode != 0:
            return True  # can't compare: don't churn a working container
        return running.stdout.strip() == current.stdout.strip()

    def pinned_address(self) -> str:
        """The gateway's fixed address on the agent network: the first host
        after docker's `.1` router."""
        return str(ipaddress.ip_network(self._subnet, strict=False).network_address + 2)

    def _at_pinned_address(self) -> bool:
        try:
            return self.address() == self.pinned_address()
        except GatewayError:
            return False

    def _forwarding_off(self) -> bool:
        """True iff the running gateway was started with IP forwarding off.
        One from before that is recreated."""
        proc = run_docker([
            "docker", "inspect", "--format",
            f'{{{{index .HostConfig.Sysctls "{_IP_FORWARD}"}}}}', self.name,
        ])
        return proc.returncode == 0 and proc.stdout.strip() == "0"

    def _bound_to(self, orchestrator_url: str, gateway_token: str) -> bool:
        """True iff the running gateway uses `orchestrator_url` and
        `gateway_token`. Anything unreadable counts as unbound."""
        proc = run_docker(["docker", "inspect", "--format", "{{json .Config.Env}}", self.name])
        if proc.returncode != 0:
            return False
        try:
            entries = json.loads(proc.stdout or "null") or []
        except json.JSONDecodeError:
            return False
        env = dict(e.split("=", 1) for e in entries if isinstance(e, str) and "=" in e)
        return (env.get("BOT_BOTTLE_ORCHESTRATOR_URL") == orchestrator_url
                and env.get(ORCHESTRATOR_AUTH_JWT_ENV) == gateway_token)

    def _ensure_network(self) -> None:
        """Create the shared `--internal` gateway network if missing; a
        concurrent create loses harmlessly. It needs an explicit subnet for
        pinned IPs. A network that is mislabelled or not internal is replaced,
        but only once no bottle is attached."""
        inspected = run_docker([
            "docker", "network", "inspect",
            "--format", f'{{{{index .Labels "{_GATEWAY_SUBNET_LABEL}"}}}}|{{{{.Internal}}}}',
            self.network,
        ])
        if inspected.returncode == 0:
            marker, _, internal = inspected.stdout.strip().rpartition("|")
            if marker in {"", self._subnet} and internal == "true":
                return
            if internal != "true":
                self._refuse_replacing_with_bottles_attached()
            # Mislabelled or not internal: replace below.
            stale = True
        else:
            # Inspect failed. Only replace on a recognized poisoned network,
            # never on a transient error, since removing it would strand every
            # live bottle.
            err = inspected.stderr.lower()
            if "no such network" in err or "not found" in err:
                # Absent: create below.
                stale = False
            elif "parseaddr" in err:
                # Poisoned: an IPv6 subnet with a `::1/64` gateway breaks
                # docker's own `network inspect`. Replace it.
                stale = True
            else:
                # Unrecognized failure: surface it rather than mutate shared
                # state.
                raise GatewayError(
                    f"gateway network {self.network} could not be inspected: "
                    f"{inspected.stderr.strip()}"
                )
        if stale:
            # Safe: this launch recreates the gateway.
            run_docker(["docker", "rm", "--force", self.name])
            removed = run_docker(["docker", "network", "rm", self.network])
            if removed.returncode != 0 and "no such network" not in removed.stderr.lower():
                raise GatewayError(
                    f"gateway network {self.network} needs explicit subnet "
                    f"{self._subnet} but could not be replaced: "
                    f"{removed.stderr.strip()}"
                )
        proc = run_docker([
            "docker", "network", "create",
            # IPv4 only; default-enabled IPv6 can attach a subnet that breaks
            # `network inspect`.
            "--ipv6=false",
            "--internal",
            "--subnet", self._subnet,
            "--label", f"{_GATEWAY_SUBNET_LABEL}={self._subnet}",
            self.network,
        ])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"gateway network {self.network} failed to create: {proc.stderr.strip()}"
            )

    def _refuse_replacing_with_bottles_attached(self) -> None:
        """Fail closed when the network must be replaced while bottles run on
        it."""
        proc = run_docker([
            "docker", "network", "inspect",
            "--format", "{{range .Containers}}{{.Name}} {{end}}",
            self.network,
        ])
        attached = [n for n in proc.stdout.split() if n != self.name]
        if proc.returncode != 0 or attached:
            raise GatewayError(
                f"gateway network {self.network} is not `--internal`, so its "
                f"agents can reach the network without the gateway. It must be "
                f"recreated, but bottles are still attached "
                f"({', '.join(attached) or 'unknown'}). Stop them "
                f"(`bot-bottle cleanup bots`) and launch again."
            )

    def _connect_egress_network(self) -> None:
        """Create the egress network if needed and attach the gateway.
        Idempotent."""
        if run_docker(["docker", "network", "inspect", self.egress_network]).returncode != 0:
            proc = run_docker(
                ["docker", "network", "create", "--ipv6=false", self.egress_network])
            if proc.returncode != 0 and "already exists" not in proc.stderr:
                raise GatewayError(
                    f"egress network {self.egress_network} failed to create: "
                    f"{proc.stderr.strip()}"
                )
        proc = run_docker(["docker", "network", "connect", self.egress_network, self.name])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"gateway failed to join egress network {self.egress_network}: "
                f"{proc.stderr.strip()}"
            )

    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> bool:
        # Bind to this orchestrator, then bring the container up. The gateway
        # holds only the `gateway` token, never the key.
        self._orchestrator_url = orchestrator_url
        self._gateway_token = gateway_token
        # Without an orchestrator URL every daemon would exit; fail here
        # instead.
        if not self._orchestrator_url:
            raise GatewayError(
                "gateway requires an orchestrator URL to run "
                "(resolver-only data plane; no single-tenant fallback)"
            )
        if not self._gateway_token:
            raise GatewayError(
                "gateway requires a pre-minted `gateway` token to run "
                "(the orchestrator mints it; the gateway never holds the key)"
            )
        # Recreate when the running container's image is stale.
        self._ensure_network()
        if (self.is_running() and self._running_image_is_current()
                and self._at_pinned_address() and self._forwarding_off()
                and self._bound_to(orchestrator_url, gateway_token)):
            return False
        # Clear any stale container holding the fixed name.
        run_docker(["docker", "rm", "--force", self.name])
        argv = [
            "docker", "run", "--detach",
            "--name", self.name,
            "--label", GATEWAY_LABEL,
            "--network", self.network,
            # Fixed address: agents bake it into their proxy env.
            "--ip", self.pinned_address(),
            # It sits on every agent's network; it must never route between
            # them.
            "--sysctl", f"{_IP_FORWARD}=0",
            # Host directory, so the CA survives recreation and volume pruning
            # (see `host_gateway_ca_dir`).
            "--volume", f"{self._ca_mount_source}:{MITMPROXY_HOME}",
            # No DB mount: the gateway reaches state over the control-plane
            # RPC.
        ]
        run_env = dict(os.environ)
        # The daemons resolve policy against the control plane per request.
        argv += ["--env", f"BOT_BOTTLE_ORCHESTRATOR_URL={self._orchestrator_url}"]
        # The `gateway` token, as a bare `--env NAME` so the value stays off
        # argv.
        argv += ["--env", ORCHESTRATOR_AUTH_JWT_ENV]
        run_env[ORCHESTRATOR_AUTH_JWT_ENV] = self._gateway_token
        argv.append(self.image_ref)
        proc = run_docker(argv, env=run_env)
        if proc.returncode != 0:
            raise GatewayError(f"gateway failed to start: {proc.stderr.strip()}")
        # Docker attaches one network at `run`; connect the egress and control
        # networks after. The daemons retry until then.
        self._connect_egress_network()
        if self._control_network:
            self._connect_control_network()
        return True

    def _connect_control_network(self) -> None:
        """Create the control network if needed and attach the gateway.
        Idempotent."""
        if run_docker(["docker", "network", "inspect", self._control_network]).returncode != 0:
            proc = run_docker(["docker", "network", "create", "--internal", self._control_network])
            if proc.returncode != 0 and "already exists" not in proc.stderr:
                raise GatewayError(
                    f"control network {self._control_network} failed to create: "
                    f"{proc.stderr.strip()}"
                )
        proc = run_docker(["docker", "network", "connect", self._control_network, self.name])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"gateway failed to join control network {self._control_network}: "
                f"{proc.stderr.strip()}"
            )

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's CA PEM. mitmproxy writes it shortly after start, so
        this polls up to `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            proc = run_docker(["docker", "exec", self.name, "cat", GATEWAY_CA_CERT])
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout
            if time.monotonic() >= deadline:
                raise GatewayError(
                    f"gateway CA cert not available after {timeout:g}s: "
                    f"{proc.stderr.strip() or 'empty'}"
                )
            time.sleep(CA_POLL_SECONDS)

    def persisted_ca_dir(self) -> Path | None:
        # A named volume (CI) isn't a host path to clear.
        source = Path(self._ca_mount_source)
        return source if source.is_absolute() else None

    def stop(self) -> None:
        """Remove the gateway container. Idempotent."""
        proc = run_docker(["docker", "rm", "--force", self.name])
        if (proc.returncode != 0 and "No such container" not in proc.stderr
                and daemon_running()):
            raise GatewayError(f"gateway failed to stop: {proc.stderr.strip()}")

    def address(self) -> str:
        """The gateway's IPv4 on the shared gateway network. Bottles launched
        before per-agent networks proxy through it; newer ones proxy through
        the gateway's address on their own network."""
        proc = run_docker([
            "docker", "inspect", "--format",
            f'{{{{(index .NetworkSettings.Networks "{self.network}").IPAddress}}}}',
            self.name,
        ])
        ip = proc.stdout.strip()
        if proc.returncode != 0 or not ip:
            raise GatewayError(
                f"gateway {self.name} has no address on {self.network}: "
                f"{proc.stderr.strip()}"
            )
        return ip

    def provisioning_transport(self) -> GatewayTransport:
        """The exec/cp transport for git-gate provisioning."""
        return DockerGatewayTransport(self.name)
