from __future__ import annotations

import os
import time
from pathlib import Path

from ...control_auth import ROLE_GATEWAY, mint
from ...docker_cmd import run_docker
from ...paths import (
    CONTROL_AUTH_JWT_ENV,
    host_control_plane_token,
    host_gateway_ca_dir,
)
from ...gateway import (
  Gateway, GATEWAY_IMAGE, GATEWAY_NAME, GATEWAY_NETWORK, GATEWAY_DOCKERFILE,
  REPO_ROOT, GATEWAY_LABEL, MITMPROXY_HOME, DEFAULT_CA_TIMEOUT_SECONDS,
  CA_POLL_SECONDS, GATEWAY_CA_CERT, GatewayError
)

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
        orchestrator_url: str = "",
        build_context: Path | None = None,
        dockerfile: str | None = GATEWAY_DOCKERFILE,
        host_port_bindings: tuple[int, ...] = (),
    ) -> None:
        self.image_ref = image_ref
        self.name = name
        self.network = network
        # The control-plane URL the gateway's data plane resolves per bottle
        # against — reached by container name over docker DNS on the shared
        # network (container↔container, no host firewall). Mandatory to *run*
        # the gateway (see `ensure_running`); empty is tolerated only for the
        # construct-then-read-CA path (`ca_cert_pem` on an already-running
        # container), which never launches a container.
        self._orchestrator_url = orchestrator_url
        self._build_context = build_context or REPO_ROOT
        self._dockerfile = dockerfile
        # Ports published on the host (0.0.0.0). Used by the Firecracker
        # backend's dev-harness gateway so VMs can reach it via their TAP link;
        # Docker's DNAT + the nft `ct status dnat accept` rule handle the rest.
        self._host_port_bindings = host_port_bindings

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
        argv = ["docker", "build", "-t", self.image_ref,
                "-f", str(self._build_context / self._dockerfile),
                str(self._build_context)]
        if os.environ.get("BOT_BOTTLE_NO_CACHE"):
            argv.insert(2, "--no-cache")
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
        Docker picks the subnet; the launcher reads it back to allocate IPs."""
        if run_docker(["docker", "network", "inspect", self.network]).returncode == 0:
            return
        proc = run_docker(["docker", "network", "create", self.network])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"gateway network {self.network} failed to create: {proc.stderr.strip()}"
            )

    def ensure_running(self) -> None:
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
        # Recreate when the running container's image is stale (a rebuild),
        # so source changes to the gateway's flat daemons take effect — not
        # just when the container is absent.
        if self.is_running() and self._running_image_is_current():
            return
        self._ensure_network()
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
            "--volume", f"{host_gateway_ca_dir()}:{MITMPROXY_HOME}",
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
        argv += ["--env", CONTROL_AUTH_JWT_ENV]
        run_env[CONTROL_AUTH_JWT_ENV] = mint(ROLE_GATEWAY, host_control_plane_token())
        argv.append(self.image_ref)
        proc = run_docker(argv, env=run_env)
        if proc.returncode != 0:
            raise GatewayError(f"gateway failed to start: {proc.stderr.strip()}")

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

    def stop(self) -> None:
        proc = run_docker(["docker", "rm", "--force", self.name])
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            raise GatewayError(f"gateway failed to stop: {proc.stderr.strip()}")