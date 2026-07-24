"""The consolidated per-host gateway (PRD 0070).

The core consolidation win: **one** persistent gateway per host, shared by
every bottle, instead of a gateway per bottle. It's safe to share
because the attribution invariant (source IP + identity token, see
`registry`) lets the gateway attribute each request to the right bottle —
so per-bottle policy lives in one long-lived process keyed on who's calling.

`Gateway` is the backend-neutral lifecycle contract (mirrors `LaunchBroker`):
ensure the single instance is up, report it, tear it down. `DockerGateway`
is the docker implementation; a firecracker gateway VM slots in later.

The defining behaviour is **idempotent singleton**: `ensure_running` starts
the instance if absent and is a no-op if it's already up, so N bottle
launches never spawn N gateways.
"""

from __future__ import annotations

import abc
import os
import time
from pathlib import Path

from ..docker_cmd import run_docker
from ..paths import (
    CONTROL_PLANE_TOKEN_ENV,
    host_control_plane_token,
    host_gateway_ca_dir,
)

# The gateway's mitmproxy writes its CA a beat after the container starts, so
# reads poll for it rather than assuming it's there on a fresh launch.
_CA_POLL_SECONDS = 0.5
DEFAULT_CA_TIMEOUT_SECONDS = 30.0

GATEWAY_NAME = "bot-bottle-orch-gateway"
GATEWAY_LABEL = "bot-bottle-orch-gateway=1"
# The single user-defined network the gateway and every agent bottle share.
# Agents attach here with a pinned IP and reach the gateway's egress /
# git-http / supervise ports by its address — no host port publishing, and
# the source IP the gateway attributes by is the address on this network.
GATEWAY_NETWORK = "bot-bottle-gateway"

# mitmproxy's CA dir in the bundle. The host's gateway-CA dir (see
# `host_gateway_ca_dir`) is bind-mounted here so the gateway's self-generated
# CA stays STABLE across container recreation — every agent installs this one
# CA to trust the shared gateway's TLS interception, so it must not rotate when
# the gateway restarts. A host bind-mount rather than a named volume: a named
# volume is silently wiped by `docker volume prune`, minting a fresh CA that
# breaks every running bottle (issue #450).
MITMPROXY_HOME = "/home/mitmproxy/.mitmproxy"
GATEWAY_CA_CERT = f"{MITMPROXY_HOME}/mitmproxy-ca-cert.pem"

# The CA material mitmproxy writes into its confdir. mitmproxy reuses these on
# startup when present and generates them only on first run, so persisting them
# is what makes the CA stable; deleting them (see `rotate_gateway_ca`) forces a
# fresh CA on the next start. `mitmproxy-ca.pem` (cert + private key) is the
# signing identity; the rest are derived encodings agents/clients consume.
GATEWAY_CA_GLOB = "mitmproxy-ca*"

# The gateway data-plane image + its Dockerfile. Kept as a local constant
# rather than imported from the backend layer, which would drag
# the whole backend layer into the lean orchestrator (see #359); unify when
# that lands. Env override matches the backend's BOT_BOTTLE_GATEWAY_IMAGE.
GATEWAY_IMAGE = os.environ.get("BOT_BOTTLE_GATEWAY_IMAGE", "bot-bottle-gateway:latest")
GATEWAY_DOCKERFILE = "Dockerfile.gateway"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def rotate_gateway_ca(ca_dir: Path | None = None) -> list[Path]:
    """Delete the persisted mitmproxy CA so the next gateway start mints a
    fresh one — the explicit, deliberate CA-rollover path (issue #450).

    Persistence keeps the CA stable across restarts precisely because mitmproxy
    reuses the on-disk CA; rotation is therefore just removing that material.
    Returns the files removed (empty when there was no CA yet); idempotent.

    This only clears the on-disk CA. It does NOT stop the running gateway (whose
    mitmproxy still holds the old CA in memory) or re-provision agents — the
    caller recreates the gateway to mint the new CA and re-attaches bottles.
    `rotate-ca` on the orchestrator CLI wires those steps together."""
    ca_dir = ca_dir if ca_dir is not None else host_gateway_ca_dir()
    removed: list[Path] = []
    for path in sorted(ca_dir.glob(GATEWAY_CA_GLOB)):
        path.unlink()
        removed.append(path)
    return removed


class GatewayError(Exception):
    """The shared gateway failed to build/start/stop (non-zero `docker` exit)."""


class Gateway(abc.ABC):
    """Lifecycle of the single per-host gateway. Backend-neutral."""

    name: str

    def ensure_built(self) -> None:
        """Ensure the gateway's image / rootfs exists, building it if needed.
        Default: nothing to build (e.g. a stub or a pre-pulled image)."""
        return

    @abc.abstractmethod
    def ensure_running(self) -> None:
        """Start the gateway if it isn't already up. Idempotent: a no-op
        when it's already running (that's the whole point — one per host).
        Assumes the image exists — call `ensure_built()` first."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the gateway instance is currently up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the gateway. Idempotent — absent is success."""


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
        self._build_context = build_context or _REPO_ROOT
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
        # ...and present the control-plane secret on those /resolve calls (the
        # control plane requires it). Bare `--env NAME` keeps the value off argv
        # / `docker inspect`; only the gateway (not the agent) is given it.
        argv += ["--env", CONTROL_PLANE_TOKEN_ENV]
        run_env[CONTROL_PLANE_TOKEN_ENV] = host_control_plane_token()
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
            time.sleep(_CA_POLL_SECONDS)

    def stop(self) -> None:
        proc = run_docker(["docker", "rm", "--force", self.name])
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            raise GatewayError(f"gateway failed to stop: {proc.stderr.strip()}")


__all__ = [
    "Gateway", "DockerGateway", "GatewayError", "rotate_gateway_ca",
    "GATEWAY_NAME", "GATEWAY_LABEL", "GATEWAY_IMAGE", "GATEWAY_NETWORK",
    "GATEWAY_CA_CERT", "GATEWAY_CA_GLOB",
]
