"""Install the per-bottle MITM CA into the smolmachines guest's
trust store (PRD 0023 chunk 4d).

Mirrors `backend.docker.provision.ca`: select the right CA (egress
when the bottle has routes, else pipelock), copy it to Debian's
`/usr/local/share/ca-certificates/` path,
`update-ca-certificates` to rebuild the trust bundle, and log the
fingerprint once. The selected cert depends on the agent's
HTTP_PROXY target — same logic as the docker backend, since the
agent dials the same daemons through the same bundle.

`smolvm machine exec` runs commands as root in the VM (no `-u`
flag exists; the VM init is root), so we don't need the explicit
`-u 0` the docker backend uses on its `docker exec` calls."""

from __future__ import annotations

import time

from ....log import die
from ...util import (
    AGENT_CA_BUNDLE,
    AGENT_CA_PATH,
    log_ca_fingerprint,
    select_ca_cert,
)
from ... import Bottle, ExecResult
from ..bottle_plan import SmolmachinesBottlePlan


_SIGKILL_EXIT = 128 + 9


def provision_ca(plan: SmolmachinesBottlePlan, bottle: Bottle) -> None:
    """Copy the agent-facing CA cert into the guest, rebuild the
    trust bundle, emit a one-line fingerprint log. Called from
    `BottleBackend.provision` after the smolvm guest is up."""
    cert_host_path, label = select_ca_cert(plan.egress_plan, plan.proxy_plan)

    bottle.cp_in(str(cert_host_path), AGENT_CA_PATH)
    # Mode 0644 — readable to non-root tools in the guest.
    # update-ca-certificates rebuilds the bundle at AGENT_CA_BUNDLE,
    # which is what curl / Python ssl / OpenSSL-based tools read by
    # default. The env trio (NODE_EXTRA_CA_CERTS / SSL_CERT_FILE /
    # REQUESTS_CA_BUNDLE) on the guest_env covers Node + Python
    # `requests` / libraries that don't load the system bundle.
    #
    r = _install_ca(bottle)
    if r.returncode == _SIGKILL_EXIT:
        # smolvm/libkrun can SIGKILL an otherwise-normal exec
        # during early-VM provisioning. `update-ca-certificates`
        # is idempotent, so retry the same install once after a
        # short settle delay before treating it as fatal.
        time.sleep(1.0)
        r = _install_ca(bottle)

    if r.returncode != 0:
        # update-ca-certificates not adding our cert is fatal —
        # claude-code's TLS handshake against the egress-MITM'd
        # api.anthropic.com would fail downstream. Bail early
        # with what we can see (output is captured so we can
        # surface it).
        die(
            f"update-ca-certificates didn't add the agent CA "
            f"(exit {r.returncode}): "
            f"stdout={(r.stdout or '').strip()!r} "
            f"stderr={(r.stderr or '').strip()!r}"
        )

    log_ca_fingerprint(cert_host_path, label)


def _install_ca(bottle: Bottle) -> ExecResult:
    # chown + chmod + update-ca-certificates + bundle
    # verification run in one exec so we only pay one
    # round trip; the `&&` chaining surfaces the first failure
    # as the return code. The verify check is more stable than
    # requiring "1 added" in stdout: a retry after a
    # partially-completed first run may legitimately report "0
    # added" while the cert is already installed.
    return bottle.exec(
        f"chown root:root {AGENT_CA_PATH} && "
        f"chmod 644 {AGENT_CA_PATH} && "
        f"update-ca-certificates && "
        f"openssl verify -CAfile {AGENT_CA_BUNDLE} {AGENT_CA_PATH}",
        user="root",
    )


# Re-exported for the launch/provision_ca caller + tests. The path
# constants live in the shared `backend.util` (Debian's
# `update-ca-certificates` layout is the same in both backends).
__all__ = ["AGENT_CA_BUNDLE", "AGENT_CA_PATH", "provision_ca"]
