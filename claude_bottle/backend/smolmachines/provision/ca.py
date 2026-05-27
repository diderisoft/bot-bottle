"""Install the per-bottle MITM CA into the smolmachines guest's
trust store (PRD 0023 chunk 4d).

Mirrors `backend.docker.provision.ca`: select the right CA (egress
when the bottle has routes, else pipelock), `smolvm machine cp` it
to Debian's `/usr/local/share/ca-certificates/` path,
`update-ca-certificates` to rebuild the trust bundle, and log the
fingerprint once. The selected cert depends on the agent's
HTTP_PROXY target — same logic as the docker backend, since the
agent dials the same daemons through the same bundle.

`smolvm machine exec` runs commands as root in the VM (no `-u`
flag exists; the VM init is root), so we don't need the explicit
`-u 0` the docker backend uses on its `docker exec` calls."""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path

from ....log import die, info
from ...docker.provision.ca import AGENT_CA_BUNDLE, AGENT_CA_PATH
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


def _select_ca_cert(plan: SmolmachinesBottlePlan) -> tuple[Path, str]:
    """Pick the CA cert (and a short label for the log line) that
    matches the proxy the agent's HTTP_PROXY points at. Egress-proxy
    wins when the bottle declares any routes; else pipelock.

    The launch step minted both CAs (pipelock always; egress when
    routes are declared) and stored their host paths back into the
    inner Plans via `dataclasses.replace`. If those paths are empty
    here something has gone wrong in launch's bringup."""
    if plan.egress_plan.routes:
        cert = plan.egress_plan.mitmproxy_ca_cert_only_host_path
        if cert == Path() or not cert.is_file():
            die(
                f"egress CA cert missing at {cert or '(empty)'}; "
                f"launch must have called egress_tls_init and "
                f"re-bound the plan before provision"
            )
        return cert, "egress"
    cert = plan.proxy_plan.ca_cert_host_path
    if not cert or not cert.is_file():
        die(
            f"pipelock CA cert missing at {cert or '(empty)'}; "
            f"launch must have called pipelock_tls_init and re-bound "
            f"the plan before provision"
        )
    return cert, "pipelock"


def provision_ca(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Copy the agent-facing CA cert into the guest, rebuild the
    trust bundle, emit a one-line fingerprint log. Called from
    `BottleBackend.provision` after the smolvm guest is up."""
    cert_host_path, label = _select_ca_cert(plan)

    _smolvm.machine_cp(str(cert_host_path), f"{target}:{AGENT_CA_PATH}")
    # Mode 0644 — readable to non-root tools in the guest.
    # update-ca-certificates rebuilds the bundle at AGENT_CA_BUNDLE,
    # which is what curl / Python ssl / OpenSSL-based tools read by
    # default. The env trio (NODE_EXTRA_CA_CERTS / SSL_CERT_FILE /
    # REQUESTS_CA_BUNDLE) on the guest_env covers Node + Python
    # `requests` / libraries that don't load the system bundle.
    _smolvm.machine_exec(target, ["chmod", "644", AGENT_CA_PATH])
    _smolvm.machine_exec(target, ["update-ca-certificates"])

    # Stdlib SHA-256 of the cert's DER bytes — the standard
    # fingerprint form. Never the private key.
    der = ssl.PEM_cert_to_DER_cert(cert_host_path.read_text())
    fingerprint = hashlib.sha256(der).hexdigest()
    info(f"{label} ca fingerprint: sha256:{fingerprint[:32]}...")


# Re-exported for the launch/provision_ca caller + tests. The path
# constants come from the docker module because they're tied to
# Debian's `update-ca-certificates` layout — same in both backends
# since both guest images are Debian-family.
__all__ = ["AGENT_CA_BUNDLE", "AGENT_CA_PATH", "provision_ca"]
