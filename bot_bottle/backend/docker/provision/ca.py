"""Install the per-bottle egress MITM CA into the agent container's
trust store.

By the time this provisioner runs, `egress_tls_init` has generated
the egress CA and the path is re-bound into `plan.egress_plan`.

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

from ... import Bottle
from ...util import AGENT_CA_PATH, log_ca_fingerprint, select_ca_cert
from ..bottle_plan import DockerBottlePlan


def provision_ca(plan: DockerBottlePlan, bottle: Bottle) -> None:
    """Copy the agent-facing CA cert into the agent, rebuild the
    trust bundle, emit a one-line fingerprint log. Called from
    `BottleBackend.provision` after the agent container is up."""
    cert_host_path, label = select_ca_cert(plan.egress_plan)

    bottle.cp_in(str(cert_host_path), AGENT_CA_PATH)
    bottle.exec(
        f"chmod 644 {AGENT_CA_PATH} && update-ca-certificates",
        user="root",
    )

    log_ca_fingerprint(cert_host_path, label)
