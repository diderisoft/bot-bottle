"""DockerEgress — the Docker-specific lifecycle for the
per-bottle egress sidecar (PRD 0017). Inherits the platform-
agnostic prepare step (route lift + routes.yaml render + token-env
map derivation) from `Egress`.

Chunks 1+2 of the PRD: the lifecycle is implemented and wired into
launch.py — cred-proxy is gone. Chunk 3 retargets the cred-proxy-
block remediation flow (PRD 0014)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...egress import (
    EGRESS_HOSTNAME,
    EGRESS_ROUTES_IN_CONTAINER,
    Egress,
    EgressPlan,
    egress_resolve_token_values,
)
from ...log import die, info, warn
from . import util as docker_mod




EGRESS_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_EGRESS_IMAGE",
    "claude-bottle-egress:latest",
)

EGRESS_DOCKERFILE = "Dockerfile.egress"

# Listening port inside the sidecar. The agent's HTTP_PROXY env var
# resolves to `http://egress:<port>`.
EGRESS_PORT = int(os.environ.get("CLAUDE_BOTTLE_EGRESS_PORT", "9099"))

# In-container path for mitmproxy's CA. The format is a single PEM
# file holding BOTH the cert and the private key, concatenated. The
# upstream-trust CA (pipelock's, so egress trusts the upstream
# leg) is a separate file because pipelock keeps a different CA on
# its end.
EGRESS_CA_IN_CONTAINER = "/home/mitmproxy/.mitmproxy/mitmproxy-ca.pem"
EGRESS_PIPELOCK_CA_IN_CONTAINER = (
    "/home/mitmproxy/.mitmproxy/pipelock-ca.pem"
)

# Repo root, for `docker build` context. Resolved from this file's
# location: claude_bottle/backend/docker/egress.py → repo root.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def egress_container_name(slug: str) -> str:
    return f"claude-bottle-egress-{slug}"


def egress_url() -> str:
    """Base URL the agent will dial via HTTP_PROXY (chunk 2). Stable
    across bottles because the sidecar attaches `--network-alias
    egress` on the internal network; the container name (which
    carries the slug) is not referenced by agent-side config."""
    return f"http://{EGRESS_HOSTNAME}:{EGRESS_PORT}"


def build_egress_image() -> None:
    """Build the egress image from `Dockerfile.egress`.
    Called by `DockerEgress.start`; exposed at module level so
    integration tests can build it without running the full launch
    pipeline."""
    docker_mod.build_image(
        EGRESS_IMAGE, _REPO_DIR, dockerfile=EGRESS_DOCKERFILE,
    )


def egress_tls_init(stage_dir: Path) -> tuple[Path, Path]:
    """Mint the per-bottle egress MITM CA via host `openssl req`.

    Returns `(mitmproxy_pem, cert_only_pem)`:
      - `mitmproxy_pem` is the single-PEM concat (cert + key)
        mitmproxy reads from `~/.mitmproxy/mitmproxy-ca.pem`.
      - `cert_only_pem` is the cert alone — installed into the agent's
        trust store by `provision_ca` so the agent trusts the bumped
        CONNECT cert egress presents.

    Why openssl req (not the pipelock binary's `tls init`):
    pipelock's CA generator stamps a non-standard `Subject Key
    Identifier` on the CA (random rather than SHA-1 of the pubkey).
    mitmproxy computes the `Authority Key Identifier` on each leaf
    it mints as SHA-1(issuer's pubkey). openssl's chain validator
    uses the leaf's AKI to find the issuer cert by SKI; pipelock's
    SKI doesn't match → openssl reports "unable to get local issuer
    certificate" even though the CA is right there in the trust
    store. openssl req's `subjectKeyIdentifier=hash` extension uses
    SHA-1(pubkey), matching mitmproxy's computation.

    Both files live under `<stage_dir>/egress-ca/` (mode 644 —
    `docker cp` preserves the mode into the container, where the
    mitmproxy user (uid 1000) reads them; the host stage_dir is
    mode 700 so the private key isn't world-exposed)."""
    work = stage_dir / "egress-ca"
    work.mkdir(exist_ok=True)
    key_path = work / "ca-key.pem"
    cert_path = work / "ca.pem"
    cnf_path = work / "ca.cnf"

    # RSA-2048 — broad mitmproxy compatibility (its default leaf-cert
    # config matches RSA CAs without surprise), and openssl req's
    # default behavior here is exactly what we want.
    keygen = subprocess.run(
        ["openssl", "genrsa", "-out", str(key_path), "2048"],
        capture_output=True, text=True, check=False,
    )
    if keygen.returncode != 0:
        die(f"egress ca keygen failed: {keygen.stderr.strip()}")
    # Standalone private key — never docker-cp'd, never bind-mounted
    # (mitmproxy reads the cert+key concat below). Lock to owner-
    # only so it doesn't sit at the default umask on disk.
    key_path.chmod(0o600)

    # `subjectKeyIdentifier=hash` makes openssl compute the SKI as
    # SHA-1(pubkey), matching how mitmproxy computes the AKI on the
    # leaves it later mints. Without this, chain validation breaks
    # despite the CA being present in the trust store.
    cnf_path.write_text(
        "[req]\n"
        "distinguished_name = req_dn\n"
        "prompt = no\n"
        "x509_extensions = v3_ca\n"
        "\n"
        "[req_dn]\n"
        "O = claude-bottle\n"
        "CN = claude-bottle egress CA\n"
        "\n"
        "[v3_ca]\n"
        "basicConstraints = critical, CA:TRUE\n"
        "keyUsage = critical, keyCertSign, cRLSign\n"
        "subjectKeyIdentifier = hash\n"
    )
    cnf_path.chmod(0o644)

    req = subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes",
         "-key", str(key_path),
         "-sha256", "-days", "365",
         "-config", str(cnf_path),
         "-out", str(cert_path)],
        capture_output=True, text=True, check=False,
    )
    if req.returncode != 0:
        die(f"egress ca cert generation failed: {req.stderr.strip()}")

    cert_path.chmod(0o644)
    # mitmproxy reads cert + key from a single concatenated PEM file.
    # This file IS bind-mounted into the egress container (chunk 3+),
    # where mitmproxy runs as uid 1000 — so the host file has to be
    # world-readable for the container's user to read it through the
    # mount. Owner-only mode on the parent dir (state/<slug>/, under
    # ~/.claude-bottle which inherits ~'s 0o700) is what actually
    # restricts who can reach this file on the host.
    mitm = work / "mitmproxy-ca.pem"
    mitm.write_bytes(cert_path.read_bytes() + key_path.read_bytes())
    mitm.chmod(0o644)
    return (mitm, cert_path)


class DockerEgress(Egress):
    """Brings the egress sidecar up and down via Docker."""

    def start(self, plan: EgressPlan) -> str:
        """Boot the egress sidecar:
          1. Resolve every host TokenRef env var into a concrete
             value. Fails early if any are unset.
          2. Build the egress image (no-op when cache is hot).
          3. `docker create` on the internal network with
             `--network-alias egress`, the `HTTPS_PROXY=pipelock`
             env (so the upstream leg traverses pipelock), the
             `EGRESS_UPSTREAM_CA` env pointing at the in-container
             pipelock-CA path (so mitmproxy trusts pipelock's MITM),
             and one `-e EGRESS_TOKEN_N` flag per token slot.
             Secret values arrive via subprocess env, never argv.
          4. `docker cp` the routes.yaml, mitmproxy CA (cert+key
             concat), and pipelock CA (cert only) into the container.
          5. Attach to the per-agent egress network so the proxy can
             reach pipelock.
          6. `docker start`.
        Returns the container name (the target passed to `.stop`)."""
        if not plan.routes:
            die("DockerEgress.start called with no routes; caller should skip")
        if not plan.internal_network or not plan.egress_network:
            die(
                "DockerEgress.start: internal_network / egress_network must be "
                "populated on the plan before start"
            )
        if not plan.routes_path.is_file():
            die(
                f"egress routes file missing at {plan.routes_path}; "
                f"Egress.prepare must run first"
            )
        if plan.mitmproxy_ca_host_path == Path() or not plan.mitmproxy_ca_host_path.is_file():
            die(
                f"DockerEgress.start: mitmproxy CA missing at "
                f"{plan.mitmproxy_ca_host_path}; egress_tls_init must run first"
            )
        # pipelock CA + upstream proxy URL: both must be present (we
        # use HTTPS_PROXY=pipelock with pipelock's own MITM CA on the
        # upstream leg) or both absent (egress goes direct, for
        # standalone integration tests that don't bring pipelock up).
        route_via_pipelock = bool(plan.pipelock_proxy_url) or plan.pipelock_ca_host_path != Path()
        if route_via_pipelock:
            if not plan.pipelock_proxy_url:
                die(
                    "DockerEgress.start: pipelock_ca_host_path is set but "
                    "pipelock_proxy_url is empty; populate both or neither."
                )
            if not plan.pipelock_ca_host_path.is_file():
                die(
                    f"DockerEgress.start: pipelock CA missing at "
                    f"{plan.pipelock_ca_host_path}; pipelock_tls_init must run first"
                )

        # Resolve host env vars into concrete values. Must happen at
        # start time (not prepare) — the values flow into the sidecar's
        # environ via subprocess env. The plan never holds them.
        token_values = egress_resolve_token_values(
            plan.token_env_map, dict(os.environ),
        )

        build_egress_image()

        name = egress_container_name(plan.slug)
        info(f"starting egress sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            "--network-alias", EGRESS_HOSTNAME,
        ]
        if route_via_pipelock:
            # Route egress's outbound traffic through pipelock
            # so the egress allowlist + DLP body scanner apply to
            # the egress → upstream leg. Pipelock MITMs each
            # handshake with its per-bottle CA, which is docker-cp'd
            # in below and pointed to via the EGRESS_UPSTREAM_CA
            # env (entrypoint conditionally adds the matching --set
            # flag).
            #
            # EGRESS_UPSTREAM_PROXY is the mechanism: mitmproxy
            # does NOT honor HTTPS_PROXY env vars on its outbound
            # side (it's a proxy server, not a client). The
            # entrypoint reads this env and switches mitmdump to
            # `--mode upstream:<URL>` so all post-MITM traffic
            # CONNECTs to pipelock instead of going direct. The
            # HTTPS/HTTP_PROXY env vars below are kept for any
            # bundled client libraries (mitmproxy plugin requests,
            # etc.) that might honor them — harmless if ignored.
            create_args.extend([
                "-e", f"EGRESS_UPSTREAM_PROXY={plan.pipelock_proxy_url}",
                "-e", f"HTTPS_PROXY={plan.pipelock_proxy_url}",
                "-e", f"HTTP_PROXY={plan.pipelock_proxy_url}",
                "-e", "NO_PROXY=localhost,127.0.0.1",
                "-e", f"EGRESS_UPSTREAM_CA={EGRESS_PIPELOCK_CA_IN_CONTAINER}",
            ])
        # One -e flag per token slot; values arrive via subprocess env.
        # docker create with `-e NAME` (no =VALUE) reads NAME from the
        # current process env at create time. We pass `env=child_env`
        # to subprocess.run so the value comes from token_values, not
        # the host's os.environ directly — keeps the resolver in one
        # place and lets egress_resolve_token_values surface
        # missing-env errors with a clear hint.
        for token_env in sorted(plan.token_env_map.keys()):
            create_args.extend(["-e", token_env])
        create_args.append(EGRESS_IMAGE)

        child_env: dict[str, str] = {**os.environ, **token_values}

        create_result = subprocess.run(
            create_args, capture_output=True, text=True, env=child_env, check=False,
        )
        if create_result.returncode != 0:
            die(
                f"failed to create egress sidecar {name}: "
                f"{create_result.stderr.strip()}"
            )

        # routes.yaml also lands inside the container; bump to 644
        # for the same reason as the CAs — mitmproxy user (uid 1000)
        # has to read it. Host stage_dir is mode 700 so the file
        # isn't actually exposed to other host users.
        plan.routes_path.chmod(0o644)
        # Pipelock CA: pipelock itself runs as root so its in-pipelock
        # copy doesn't care about mode, but egress's mitmproxy
        # user does. Bump on the host so docker cp into egress
        # carries world-readable.
        if route_via_pipelock:
            plan.pipelock_ca_host_path.chmod(0o644)
        cps: list[tuple[Path, str, str]] = [
            (plan.routes_path, EGRESS_ROUTES_IN_CONTAINER, "routes.yaml"),
            (plan.mitmproxy_ca_host_path, EGRESS_CA_IN_CONTAINER, "mitmproxy CA"),
        ]
        if route_via_pipelock:
            cps.append((
                plan.pipelock_ca_host_path,
                EGRESS_PIPELOCK_CA_IN_CONTAINER,
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
                f"failed to attach egress sidecar {name} to egress network "
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
                f"failed to start egress sidecar {name}: "
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
                    f"failed to remove egress sidecar {target}; "
                    f"clean up with 'docker rm -f {target}'"
                )
