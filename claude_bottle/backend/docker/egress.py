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

from ...egress import Egress
from ...log import die
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
    """Docker-flavored Egress: inherits `.prepare()` from the base.
    Container lifecycle is owned by compose; per-container
    `.start()` / `.stop()` were removed in PRD 0024 chunk 3."""
