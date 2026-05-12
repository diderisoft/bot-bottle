"""Install pipelock's per-bottle CA into the agent container's trust
store (PRD 0006).

By the time this provisioner runs, `pipelock_tls_init` has generated
a fresh CA into `plan.stage_dir/pipelock-ca/` and the pipelock sidecar
is up with `tls_interception: { enabled: true }` referencing the
in-container CA paths. This step makes the agent trust certs signed
by that CA so the agent's TLS handshake with the bumped CONNECT
succeeds.

Cert lands on Debian's standard source path
(`/usr/local/share/ca-certificates/`); `update-ca-certificates`
rebuilds `/etc/ssl/certs/ca-certificates.crt`, which is what curl,
Python `ssl`, and OpenSSL-based tools all read by default. The env
trio set on the agent's `docker run` covers Node
(`NODE_EXTRA_CA_CERTS`) and Python `requests` /
`SSL_CERT_FILE`-honoring libraries that don't load the system
bundle.

The fingerprint is computed via stdlib (`ssl.PEM_cert_to_DER_cert`
+ `hashlib.sha256`) and logged once to stderr. The private key
stays on the host (under `stage_dir`) until teardown wipes the
stage dir; nothing in the agent ever sees it."""

from __future__ import annotations

import hashlib
import ssl
import subprocess

from ....log import info
from ..bottle_plan import DockerBottlePlan


# Debian-family path for sources that `update-ca-certificates` reads.
# Bundle path is what the command rebuilds and what every standard
# TLS consumer in the image reads.
AGENT_CA_PATH = "/usr/local/share/ca-certificates/claude-bottle-pipelock-ca.crt"
AGENT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def provision_ca(plan: DockerBottlePlan, target: str) -> None:
    """Copy pipelock's CA cert into the agent, rebuild the trust
    bundle, emit a one-line fingerprint log. Called from
    `BottleBackend.provision` after the agent container is up."""
    container = target
    cert_host_path = plan.proxy_plan.ca_cert_host_path
    if not cert_host_path or not cert_host_path.is_file():
        # Defensive: provision runs after launch wires CA paths
        # onto the plan via dataclasses.replace; an empty path here
        # would mean that wiring was skipped.
        from ....log import die
        die(
            f"pipelock CA cert missing at {cert_host_path or '(empty)'}; "
            f"launch must have called pipelock_tls_init and re-bound "
            f"the plan before provision"
        )

    subprocess.run(
        ["docker", "cp", str(cert_host_path), f"{container}:{AGENT_CA_PATH}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "chmod", "644", AGENT_CA_PATH],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "update-ca-certificates"],
        stdout=subprocess.DEVNULL,
        check=True,
    )

    # Stdlib SHA-256 of the cert's DER bytes — the standard
    # fingerprint form. Never the private key.
    der = ssl.PEM_cert_to_DER_cert(cert_host_path.read_text())
    fingerprint = hashlib.sha256(der).hexdigest()
    info(f"pipelock ca fingerprint: sha256:{fingerprint[:32]}...")
