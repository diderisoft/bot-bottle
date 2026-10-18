from __future__ import annotations

import os
import time
from pathlib import Path

from .util import run_docker
from .gateway_transport import DockerGatewayTransport
from ...paths import (
    ORCHESTRATOR_AUTH_JWT_ENV,
    host_gateway_ca_dir,
)
from ... import resources
from ...gateway import (
  Gateway, GatewayTransport, GATEWAY_IMAGE, GATEWAY_NAME, GATEWAY_NETWORK,
  GATEWAY_DOCKERFILE, GATEWAY_LABEL, MITMPROXY_HOME,
  DEFAULT_CA_TIMEOUT_SECONDS, CA_POLL_SECONDS, GATEWAY_CA_CERT, GatewayError
)

DEFAULT_GATEWAY_SUBNET = "10.242.255.0/24"
_GATEWAY_SUBNET_LABEL = "bot-bottle.gateway-subnet"


class DockerGateway(Gateway):
    """The consolidated gateway as a single, fixed-name Docker container.

    `image_ref` defaults to the gateway data-plane image; `ensure_built`
    builds it from `Dockerfile.gateway` when it's missing. (Note: slice 5
    builds + launches the bundle container; wiring its per-bottle,
    source-IP-keyed config is a later slice — see PRD 0070.)"""

    def __init__(
        self,
        image_ref: str = GATEWAY_IMAGE,
        *,
        name: str = GATEWAY_NAME,
        network: str = GATEWAY_NETWORK,
        control_network: str = "",
        build_context: Path | None = None,
        dockerfile: str | None = GATEWAY_DOCKERFILE,
        host_port_bindings: tuple[int, ...] = (),
        ca_mount_source: str | Path | None = None,
        subnet: str | None = None,
    ) -> None:
        self.image_ref = image_ref
        self.name = name
        self.network = network
        # When set, the gateway is dual-homed: it also joins this `--internal`
        # control network (shared only with the orchestrator) so it can resolve
        # `orchestrator_url` by container name over docker DNS. Agents are never
        # on it, so they get no route to the control plane (PRD 0070).
        self._control_network = control_network
        # The orchestrator binding — set by `connect_to_orchestrator` (the
        # control-plane URL the data plane resolves against, and the pre-minted
        # `gateway` token it presents; the gateway never mints, so it never
        # holds the signing key — #469). Empty until connected; `ca_cert_pem` /
        # `address` / `stop` work on an already-running gateway without it.
        self._orchestrator_url = ""
        self._gateway_token = ""
        # Resolved lazily in ensure_built() so merely constructing a gateway to
        # read its CA never stages a build root from an installed wheel.
        self._build_context = build_context
        self._dockerfile = dockerfile
        # Ports published on the host (0.0.0.0). Used by the Firecracker
        # backend's dev-harness gateway so VMs can reach it via their TAP link;
        # Docker's DNAT + the nft `ct status dnat accept` rule handle the rest.
        self._host_port_bindings = host_port_bindings
        self._subnet = (
            subnet
            or os.environ.get("BOT_BOTTLE_DOCKER_GATEWAY_SUBNET", "").strip()
            or DEFAULT_GATEWAY_SUBNET
        )
        configured_ca = os.environ.get("BOT_BOTTLE_DOCKER_CA_MOUNT", "").strip()
        self._ca_mount_source = str(
            ca_mount_source or configured_ca or host_gateway_ca_dir()
        )

    def image_exists(self) -> bool:
        return run_docker(["docker", "image", "inspect", self.image_ref]).returncode == 0

    def ensure_built(self) -> None:
        """Build the bundle image from its Dockerfile, **cache-aware** — cheap
        (a cache check) when nothing changed, a real rebuild when the flat
        sources (egress addon / git-http / policy_resolver / supervise) moved.

        This deliberately builds every time rather than build-if-missing: the
        per-bottle model kept the image fresh via compose's `build:` on up, and
        a stale image silently runs the OLD single-tenant daemons. No-op only
        when no dockerfile is configured (a pre-pulled image). BOT_BOTTLE_NO_CACHE
        forces a full rebuild (parity with `start --no-cache`)."""
        if self._dockerfile is None:
            return
        context = self._build_context or resources.build_root()
        argv = ["docker", "build", "-t", self.image_ref,
                "-f", str(context / self._dockerfile),
                str(context)]
        if os.environ.get("BOT_BOTTLE_NO_CACHE"):
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
        """True iff the running gateway was created from the *current*
        `image_ref`. When `ensure_built` rebuilds the image (a source change),
        the running container is still the OLD image running the OLD flat
        daemons — so this is how a rebuild actually takes effect: a mismatch
        means recreate."""
        running = run_docker(["docker", "inspect", "--format", "{{.Image}}", self.name])
        current = run_docker(["docker", "image", "inspect", "--format", "{{.Id}}", self.image_ref])
        if running.returncode != 0 or current.returncode != 0:
            return True  # can't compare → don't churn a working container
        return running.stdout.strip() == current.stdout.strip()

    def _ensure_network(self) -> None:
        """Create the shared gateway network if it doesn't exist. Idempotent —
        a concurrent create loses harmlessly (the loser sees 'already exists').
        The explicit subnet is required because bottle attribution pins source
        IPs; Docker rejects static endpoint addresses on an auto-IPAM network."""
        inspected = run_docker([
            "docker", "network", "inspect",
            "--format", f'{{{{index .Labels "{_GATEWAY_SUBNET_LABEL}"}}}}',
            self.network,
        ])
        if inspected.returncode == 0:
            marker = inspected.stdout.strip()
            if marker in {"", self._subnet}:
                return
            # Inspectable but mislabelled: the stale auto-IPAM network created
            # by older releases. Replace it below.
            stale = True
        else:
            # inspect failed. Classify by stderr — do NOT assume "not absent"
            # implies "poisoned": a transient daemon/API error, permission
            # failure, timeout, or bad context also fails here, and destroying
            # the shared gateway on that guess would tear the network out from
            # under every live bottle.
            err = inspected.stderr.lower()
            if "no such network" in err or "not found" in err:
                # Absent: nothing to replace — create it below.
                stale = False
            elif "parseaddr" in err:
                # Present but poisoned. A daemon that default-enables IPv6
                # attaches an fdd0::/64 subnet whose `::1/64` gateway trips
                # docker's own netip.ParseAddr in `network inspect`/`ls`, so the
                # command exits non-zero with that signature. A fixed release
                # never *creates* such a network, but one can survive on a
                # shared host from an older or concurrent launch — and
                # `--ipv6=false` alone can't heal it, since the create below only
                # no-ops on "already exists". Force-replace it so later reads
                # (e.g. `_network_cidr` pinning a source IP) stop failing.
                stale = True
            else:
                # Unrecognized failure: no evidence the network is malformed.
                # Surface it rather than mutate shared state on a guess.
                raise GatewayError(
                    f"gateway network {self.network} could not be inspected: "
                    f"{inspected.stderr.strip()}"
                )
        if stale:
            # Migrate the stale/poisoned network. Removing the fixed gateway is
            # safe here: this launch recreates it.
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
            # bot-bottle attribution pins IPv4 source IPs; it has no IPv6
            # support. Disable IPv6 explicitly so a daemon that default-enables
            # it (default-address-pools) can't attach an fdd0::/64 subnet — a
            # malformed `::1/64` gateway address then trips docker's own
            # ParseAddr in `network inspect`/`ls`, which poisons every launch
            # that reads this network's subnet.
            "--ipv6=false",
            "--subnet", self._subnet,
            "--label", f"{_GATEWAY_SUBNET_LABEL}={self._subnet}",
            self.network,
        ])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"gateway network {self.network} failed to create: {proc.stderr.strip()}"
            )

    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> bool:
        # Bind to this orchestrator (PRD 0070): stash the URL its daemons resolve
        # policy against + the pre-minted `gateway` token they present, then bring
        # the container up. The gateway never mints, so it holds no signing key —
        # only this token (#469).
        self._orchestrator_url = orchestrator_url
        self._gateway_token = gateway_token
        # Fail closed on a missing policy source. The data-plane daemons are
        # resolver-only now (PRD 0070) — without an orchestrator URL egress
        # raises, git-http exits 1, and supervise exits 2 — so launching a
        # gateway without one would only crash-loop its daemons. Refuse here so
        # the misconfiguration surfaces as a clear error, not a broken container.
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
        # Recreate when the running container's image is stale (a rebuild),
        # so source changes to the gateway's flat daemons take effect — not
        # just when the container is absent.
        self._ensure_network()
        if self.is_running() and self._running_image_is_current():
            return False
        # Clear any stale (stopped OR outdated-image) container holding the
        # fixed name, then start fresh. `rm --force` on an absent name is a
        # tolerated no-op.
        run_docker(["docker", "rm", "--force", self.name])
        argv = [
            "docker", "run", "--detach",
            "--name", self.name,
            "--label", GATEWAY_LABEL,
            "--network", self.network,
            # Persist the self-generated CA on the host so it survives both
            # container recreation AND docker volume pruning (agents trust it)
            # — see host_gateway_ca_dir / issue #450.
            "--volume", f"{self._ca_mount_source}:{MITMPROXY_HOME}",
            # No DB mount: the data plane (egress / supervise / git-gate) reaches
            # the supervise queue over the control-plane RPC and never opens
            # bot-bottle.db, so the gateway container gets no file handle on it
            # (PRD 0070 / issue #469).
        ]
        for port in self._host_port_bindings:
            argv += ["--publish", f"0.0.0.0:{port}:{port}"]
        run_env = dict(os.environ)
        # The gateway's egress / git / supervise daemons resolve source-IP ->
        # policy against the control plane per request (guaranteed non-empty by
        # the check above).
        argv += ["--env", f"BOT_BOTTLE_ORCHESTRATOR_URL={self._orchestrator_url}"]
        # ...presenting a role-scoped `gateway` token (a signed JWT minted from
        # the host signing key) on those calls. The gateway never receives the
        # signing key — only this pre-minted token, which it cannot rewrite into
        # a `cli` token, so a compromised data-plane process can't drive the
        # operator routes (issue #469 review). Bare `--env NAME` keeps the value
        # off argv / `docker inspect`; only the gateway (not the agent) is given it.
        argv += ["--env", ORCHESTRATOR_AUTH_JWT_ENV]
        run_env[ORCHESTRATOR_AUTH_JWT_ENV] = self._gateway_token
        argv.append(self.image_ref)
        proc = run_docker(argv, env=run_env)
        if proc.returncode != 0:
            raise GatewayError(f"gateway failed to start: {proc.stderr.strip()}")
        # Dual-home onto the control network so the daemons resolve the
        # orchestrator by name. Docker attaches only one network at `run`, so
        # the second is a `network connect` — the daemons tolerate the brief
        # pre-connect window (they retry /resolve per request).
        if self._control_network:
            self._connect_control_network()
        return True

    def _connect_control_network(self) -> None:
        """Ensure the `--internal` control network exists and attach the gateway
        to it (idempotent — an already-connected container is a tolerated
        no-op)."""
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
        """The gateway's CA certificate (PEM) that agents install to trust its
        TLS interception. mitmproxy generates it a moment after the container
        starts, so this **polls** for it (up to `timeout`) rather than assuming
        it's already there on a fresh gateway — raising only if it never
        appears."""
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
        # A host bind mount; a named volume (CI) isn't a host path to clear.
        source = Path(self._ca_mount_source)
        return source if source.is_absolute() else None

    def stop(self) -> None:
        proc = run_docker(["docker", "rm", "--force", self.name])
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            raise GatewayError(f"gateway failed to stop: {proc.stderr.strip()}")

    def address(self) -> str:
        """The gateway's IPv4 on the agent-facing network (`self.network`) — the
        proxy target agents dial for egress / git-http / supervise. This is
        *not* the control network: agents share only this network with the
        gateway, so its address here is also the source IP the gateway
        attributes each request by."""
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
        """The exec/cp transport git-gate provisioning stages per-bottle repos +
        deploy keys through (over the docker socket)."""
        return DockerGatewayTransport(self.name)
