"""DockerEgressProxy — the Docker-specific lifecycle for the
per-bottle egress-proxy sidecar (PRD 0017). Inherits the platform-
agnostic prepare step (route lift + routes.yaml render + token-env
map derivation) from `EgressProxy`.

Chunks 1+2 of the PRD: the lifecycle is implemented and wired into
launch.py — cred-proxy is gone. Chunk 3 retargets the cred-proxy-
block remediation flow (PRD 0014)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...egress_proxy import (
    EGRESS_PROXY_HOSTNAME,
    EGRESS_PROXY_ROUTES_IN_CONTAINER,
    EgressProxy,
    EgressProxyPlan,
    egress_proxy_resolve_token_values,
)
from ...log import die, info, warn
from . import util as docker_mod




EGRESS_PROXY_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_EGRESS_PROXY_IMAGE",
    "claude-bottle-egress-proxy:latest",
)

EGRESS_PROXY_DOCKERFILE = "Dockerfile.egress-proxy"

# Listening port inside the sidecar. The agent's HTTP_PROXY env var
# resolves to `http://egress-proxy:<port>`.
EGRESS_PROXY_PORT = int(os.environ.get("CLAUDE_BOTTLE_EGRESS_PROXY_PORT", "9099"))

# In-container path for mitmproxy's CA. The format is a single PEM
# file holding BOTH the cert and the private key, concatenated. The
# upstream-trust CA (pipelock's, so egress-proxy trusts the upstream
# leg) is a separate file because pipelock keeps a different CA on
# its end.
EGRESS_PROXY_CA_IN_CONTAINER = "/home/mitmproxy/.mitmproxy/mitmproxy-ca.pem"
EGRESS_PROXY_PIPELOCK_CA_IN_CONTAINER = (
    "/home/mitmproxy/.mitmproxy/pipelock-ca.pem"
)

# Repo root, for `docker build` context. Resolved from this file's
# location: claude_bottle/backend/docker/egress_proxy.py → repo root.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def egress_proxy_container_name(slug: str) -> str:
    return f"claude-bottle-egress-proxy-{slug}"


def egress_proxy_url() -> str:
    """Base URL the agent will dial via HTTP_PROXY (chunk 2). Stable
    across bottles because the sidecar attaches `--network-alias
    egress-proxy` on the internal network; the container name (which
    carries the slug) is not referenced by agent-side config."""
    return f"http://{EGRESS_PROXY_HOSTNAME}:{EGRESS_PROXY_PORT}"


def build_egress_proxy_image() -> None:
    """Build the egress-proxy image from `Dockerfile.egress-proxy`.
    Called by `DockerEgressProxy.start`; exposed at module level so
    integration tests can build it without running the full launch
    pipeline."""
    docker_mod.build_image(
        EGRESS_PROXY_IMAGE, _REPO_DIR, dockerfile=EGRESS_PROXY_DOCKERFILE,
    )


def egress_proxy_tls_init(stage_dir: Path) -> tuple[Path, Path]:
    """Mint the per-bottle egress-proxy MITM CA. Reuses the pipelock
    binary's `tls init` subcommand — a known-good RSA CA minter we
    already pin and run on this host.

    Returns `(mitmproxy_pem, cert_only_pem)`:
      - `mitmproxy_pem` is the single-PEM concat (cert + key)
        mitmproxy reads from `~/.mitmproxy/mitmproxy-ca.pem`.
      - `cert_only_pem` is the cert alone — installed into the agent's
        trust store by `provision_ca` so the agent trusts the bumped
        CONNECT cert egress-proxy presents.

    Both files live under `<stage_dir>/egress-proxy-ca/` (mode 600).
    Private keys never leave the host stage dir until
    `DockerEgressProxy.start` docker-cps the concat file into the
    sidecar; start.py's outer finally `shutil.rmtree`s the stage dir
    after teardown.

    Imported lazily inside the function so test patchers in
    pipelock-land don't need to know about us."""
    # Local import keeps the module-import graph free of a hard
    # pipelock-image dependency at top of file (we don't actually
    # need pipelock's *runtime* here, just its tls-init subcommand).
    from .pipelock import PIPELOCK_IMAGE
    work = stage_dir / "egress-proxy-ca"
    work.mkdir(exist_ok=True)
    result = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{work}:/h",
         "-e", "PIPELOCK_HOME=/h",
         PIPELOCK_IMAGE, "tls", "init"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        die(f"egress-proxy tls init failed: {result.stderr.strip()}")
    cert = work / "ca.pem"
    key = work / "ca-key.pem"
    if not cert.is_file() or not key.is_file():
        die(f"egress-proxy tls init did not produce ca files in {work}")
    # Mode 644 (not 600) so `docker cp` preserves world-readability
    # inside the container — the mitmproxy user (uid 1000) needs to
    # read the file, and the host uid `docker cp` propagates from the
    # source doesn't match. The host stage_dir is mode 700 so other
    # host users still can't traverse in; the private key isn't
    # exposed despite the file mode.
    cert.chmod(0o644)
    # mitmproxy reads cert + key from a single concatenated PEM file.
    mitm = work / "mitmproxy-ca.pem"
    mitm.write_bytes(cert.read_bytes() + key.read_bytes())
    mitm.chmod(0o644)
    return (mitm, cert)


class DockerEgressProxy(EgressProxy):
    """Brings the egress-proxy sidecar up and down via Docker."""

    def start(self, plan: EgressProxyPlan) -> str:
        """Boot the egress-proxy sidecar:
          1. Resolve every host TokenRef env var into a concrete
             value. Fails early if any are unset.
          2. Build the egress-proxy image (no-op when cache is hot).
          3. `docker create` on the internal network with
             `--network-alias egress-proxy`, the `HTTPS_PROXY=pipelock`
             env (so the upstream leg traverses pipelock), the
             `EGRESS_PROXY_UPSTREAM_CA` env pointing at the in-container
             pipelock-CA path (so mitmproxy trusts pipelock's MITM),
             and one `-e EGRESS_PROXY_TOKEN_N` flag per token slot.
             Secret values arrive via subprocess env, never argv.
          4. `docker cp` the routes.yaml, mitmproxy CA (cert+key
             concat), and pipelock CA (cert only) into the container.
          5. Attach to the per-agent egress network so the proxy can
             reach pipelock.
          6. `docker start`.
        Returns the container name (the target passed to `.stop`)."""
        if not plan.routes:
            die("DockerEgressProxy.start called with no routes; caller should skip")
        if not plan.internal_network or not plan.egress_network:
            die(
                "DockerEgressProxy.start: internal_network / egress_network must be "
                "populated on the plan before start"
            )
        if not plan.routes_path.is_file():
            die(
                f"egress-proxy routes file missing at {plan.routes_path}; "
                f"EgressProxy.prepare must run first"
            )
        if plan.mitmproxy_ca_host_path == Path() or not plan.mitmproxy_ca_host_path.is_file():
            die(
                f"DockerEgressProxy.start: mitmproxy CA missing at "
                f"{plan.mitmproxy_ca_host_path}; egress_proxy_tls_init must run first"
            )
        # pipelock CA + upstream proxy URL: both must be present (we
        # use HTTPS_PROXY=pipelock with pipelock's own MITM CA on the
        # upstream leg) or both absent (egress-proxy goes direct, for
        # standalone integration tests that don't bring pipelock up).
        route_via_pipelock = bool(plan.pipelock_proxy_url) or plan.pipelock_ca_host_path != Path()
        if route_via_pipelock:
            if not plan.pipelock_proxy_url:
                die(
                    "DockerEgressProxy.start: pipelock_ca_host_path is set but "
                    "pipelock_proxy_url is empty; populate both or neither."
                )
            if not plan.pipelock_ca_host_path.is_file():
                die(
                    f"DockerEgressProxy.start: pipelock CA missing at "
                    f"{plan.pipelock_ca_host_path}; pipelock_tls_init must run first"
                )

        # Resolve host env vars into concrete values. Must happen at
        # start time (not prepare) — the values flow into the sidecar's
        # environ via subprocess env. The plan never holds them.
        token_values = egress_proxy_resolve_token_values(
            plan.token_env_map, dict(os.environ),
        )

        build_egress_proxy_image()

        name = egress_proxy_container_name(plan.slug)
        info(f"starting egress-proxy sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            "--network-alias", EGRESS_PROXY_HOSTNAME,
        ]
        if route_via_pipelock:
            # Route egress-proxy's outbound HTTPS through pipelock so
            # the egress allowlist + DLP body scanner apply to its
            # traffic on the egress-proxy → upstream leg. Pipelock
            # MITMs each handshake with its per-bottle CA, which is
            # docker-cp'd in below and pointed to via the
            # EGRESS_PROXY_UPSTREAM_CA env (entrypoint conditionally
            # adds the matching --set flag).
            create_args.extend([
                "-e", f"HTTPS_PROXY={plan.pipelock_proxy_url}",
                "-e", f"HTTP_PROXY={plan.pipelock_proxy_url}",
                "-e", "NO_PROXY=localhost,127.0.0.1",
                "-e", f"EGRESS_PROXY_UPSTREAM_CA={EGRESS_PROXY_PIPELOCK_CA_IN_CONTAINER}",
            ])
        # One -e flag per token slot; values arrive via subprocess env.
        # docker create with `-e NAME` (no =VALUE) reads NAME from the
        # current process env at create time. We pass `env=child_env`
        # to subprocess.run so the value comes from token_values, not
        # the host's os.environ directly — keeps the resolver in one
        # place and lets egress_proxy_resolve_token_values surface
        # missing-env errors with a clear hint.
        for token_env in sorted(plan.token_env_map.keys()):
            create_args.extend(["-e", token_env])
        create_args.append(EGRESS_PROXY_IMAGE)

        child_env: dict[str, str] = {**os.environ, **token_values}

        create_result = subprocess.run(
            create_args, capture_output=True, text=True, env=child_env, check=False,
        )
        if create_result.returncode != 0:
            die(
                f"failed to create egress-proxy sidecar {name}: "
                f"{create_result.stderr.strip()}"
            )

        # routes.yaml also lands inside the container; bump to 644
        # for the same reason as the CAs — mitmproxy user (uid 1000)
        # has to read it. Host stage_dir is mode 700 so the file
        # isn't actually exposed to other host users.
        plan.routes_path.chmod(0o644)
        # Pipelock CA: pipelock itself runs as root so its in-pipelock
        # copy doesn't care about mode, but egress-proxy's mitmproxy
        # user does. Bump on the host so docker cp into egress-proxy
        # carries world-readable.
        if route_via_pipelock:
            plan.pipelock_ca_host_path.chmod(0o644)
        cps: list[tuple[Path, str, str]] = [
            (plan.routes_path, EGRESS_PROXY_ROUTES_IN_CONTAINER, "routes.yaml"),
            (plan.mitmproxy_ca_host_path, EGRESS_PROXY_CA_IN_CONTAINER, "mitmproxy CA"),
        ]
        if route_via_pipelock:
            cps.append((
                plan.pipelock_ca_host_path,
                EGRESS_PROXY_PIPELOCK_CA_IN_CONTAINER,
                "pipelock CA",
            ))
        for src, dst, label in cps:
            cp_result = subprocess.run(
                ["docker", "cp", str(src), f"{name}:{dst}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if cp_result.returncode != 0:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                die(
                    f"failed to copy {label} into {name}: "
                    f"{cp_result.stderr.strip()}"
                )

        connect_result = subprocess.run(
            ["docker", "network", "connect", plan.egress_network, name],
            capture_output=True, text=True, check=False,
        )
        if connect_result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(
                f"failed to attach egress-proxy sidecar {name} to egress network "
                f"{plan.egress_network}: {connect_result.stderr.strip()}"
            )

        start_result = subprocess.run(
            ["docker", "start", name], capture_output=True, text=True, check=False,
        )
        if start_result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(
                f"failed to start egress-proxy sidecar {name}: "
                f"{start_result.stderr.strip()}"
            )

        return name

    def stop(self, target: str) -> None:
        """Idempotent: missing container is success. `target` is the
        container name returned by `.start`."""
        if subprocess.run(
            ["docker", "inspect", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0:
            if subprocess.run(
                ["docker", "rm", "-f", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                warn(
                    f"failed to remove egress-proxy sidecar {target}; "
                    f"clean up with 'docker rm -f {target}'"
                )
