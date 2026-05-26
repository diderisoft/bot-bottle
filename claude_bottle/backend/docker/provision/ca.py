"""Install the per-bottle MITM CA into the agent container's trust
store.

Post-PRD-0017 the CA depends on the agent's HTTP_PROXY target:

  - Bottle declares `egress.routes[]` → agent's HTTP_PROXY
    points at egress; the cert the agent must trust is the
    one egress mints leaf certs with (the egress CA).
  - No egress routes → agent's HTTP_PROXY points straight at
    pipelock; the cert the agent must trust is pipelock's CA (the
    pre-cutover behavior).

By the time this provisioner runs, the corresponding `tls_init`
helper has generated the chosen CA under `plan.stage_dir`, and the
sidecar (pipelock or egress) is up referencing the
in-container CA paths.

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
from pathlib import Path

from ....log import info
from ..bottle_plan import DockerBottlePlan


# Debian-family path for sources that `update-ca-certificates` reads.
# Bundle path is what the command rebuilds and what every standard
# TLS consumer in the image reads.
AGENT_CA_PATH = "/usr/local/share/ca-certificates/claude-bottle-mitm-ca.crt"
AGENT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def _select_ca_cert(plan: DockerBottlePlan) -> tuple[Path, str]:
    """Pick the CA cert (and a short label for the log line) that
    matches the proxy the agent's HTTP_PROXY points at. Egress-proxy
    wins when the bottle declares any routes (it sits in front of
    pipelock); else pipelock."""
    if plan.egress_plan.routes:
        cert = plan.egress_plan.mitmproxy_ca_cert_only_host_path
        if cert == Path() or not cert.is_file():
            from ....log import die
            die(
                f"egress CA cert missing at {cert or '(empty)'}; "
                f"launch must have called egress_tls_init and "
                f"re-bound the plan before provision"
            )
        return cert, "egress"
    cert = plan.proxy_plan.ca_cert_host_path
    if not cert or not cert.is_file():
        from ....log import die
        die(
            f"pipelock CA cert missing at {cert or '(empty)'}; "
            f"launch must have called pipelock_tls_init and re-bound "
            f"the plan before provision"
        )
    return cert, "pipelock"


def provision_ca(plan: DockerBottlePlan, target: str) -> None:
    """Copy the agent-facing CA cert into the agent, rebuild the
    trust bundle, emit a one-line fingerprint log. Called from
    `BottleBackend.provision` after the agent container is up."""
    container = target
    cert_host_path, label = _select_ca_cert(plan)

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
    info(f"{label} ca fingerprint: sha256:{fingerprint[:32]}...")
