"""Host-side helpers shared by every backend."""

from __future__ import annotations

import hashlib
import os
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..lib.log import die, info

if TYPE_CHECKING:
    from ..egress import EgressPlan

_CA_POLL_INTERVAL = 0.5


def poll_ca_cert(fetch: Callable[[], str | None], *, timeout: float) -> str:
    """Poll `fetch` until it returns a PEM; raises `TimeoutError` after
    `timeout`."""
    deadline = time.monotonic() + timeout
    while True:
        result = fetch()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"CA cert not available after {timeout:g}s")
        time.sleep(_CA_POLL_INTERVAL)


# Debian CA layout, used by every guest image: `update-ca-certificates` reads
# AGENT_CA_PATH and rebuilds AGENT_CA_BUNDLE.
AGENT_CA_PATH = "/usr/local/share/ca-certificates/bot-bottle-mitm-ca.crt"
AGENT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

# The root PATH pinned for root execs in an agent. The agent's own PATH
# includes dirs it can write to.
ROOT_EXEC_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def install_agent_ca_script(source: str) -> str:
    """A `/bin/sh -c` script, run as root, that installs the CA PEM at `source`
    (a path, or `-` for stdin) and rebuilds the trust store."""
    read = "cat" if source == "-" else f"cat {source}"
    return (
        f"export PATH={ROOT_EXEC_PATH}; "
        "mkdir -p /usr/local/share/ca-certificates && "
        f"{read} > {AGENT_CA_PATH} && chmod 644 {AGENT_CA_PATH} && "
        + ("" if source == "-" else f"rm -f {source} && ")
        + "update-ca-certificates >/dev/null"
    )


def host_skill_dir(name: str) -> str:
    """`$HOME/.claude/skills/<name>`. Dies if HOME is unset."""
    home = os.environ.get("HOME")
    if not home:
        die("HOME not set")
    return f"{home}/.claude/skills/{name}"


def select_ca_cert(egress_plan: EgressPlan) -> tuple[Path, str]:
    """The gateway CA path and label for `provision_ca`. Launch always stages
    it, so a missing path is fatal."""
    cert = egress_plan.mitmproxy_ca_cert_only_host_path
    if cert == Path() or not cert.is_file():
        die(
            f"egress CA cert missing at {cert or '(empty)'}; "
            f"launch must have called egress_tls_init and "
            f"re-bound the plan before provision"
        )
    return cert, "egress"


def log_ca_fingerprint(cert_host_path: Path, label: str) -> None:
    """Log the cert's SHA-256 fingerprint (over DER) to stderr."""
    der = ssl.PEM_cert_to_DER_cert(cert_host_path.read_text())
    fingerprint = hashlib.sha256(der).hexdigest()
    info(f"{label} ca fingerprint: sha256:{fingerprint[:32]}...")
