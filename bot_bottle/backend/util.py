"""Cross-backend utility helpers — host-side primitives shared by
every backend implementation. Backend-specific helpers live one level
deeper (e.g. bot_bottle/backend/docker/util.py)."""

from __future__ import annotations

import hashlib
import os
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..log import die, info

if TYPE_CHECKING:
    from ..egress import EgressPlan

_CA_POLL_INTERVAL = 0.5


def poll_ca_cert(fetch: Callable[[], str | None], *, timeout: float) -> str:
    """Poll `fetch` until it returns a non-empty PEM string or `timeout` expires.

    `fetch` should return the PEM on success and `None` (or empty string) when
    the cert is not yet available. Raises `TimeoutError` if the cert never
    appears within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        result = fetch()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"CA cert not available after {timeout:g}s")
        time.sleep(_CA_POLL_INTERVAL)


# Debian-family CA layout, shared by every backend (all guest images
# are Debian-family). AGENT_CA_PATH is the source path that
# `update-ca-certificates` reads; AGENT_CA_BUNDLE is the bundle it
# rebuilds, which curl, Python `ssl`, and OpenSSL-based tools all read
# by default.
AGENT_CA_PATH = "/usr/local/share/ca-certificates/bot-bottle-mitm-ca.crt"
AGENT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def host_skill_dir(name: str) -> str:
    """Return the host-side path for a named skill:
    `$HOME/.claude/skills/<name>`. Dies if HOME is unset."""
    home = os.environ.get("HOME")
    if not home:
        die("HOME not set")
    return f"{home}/.claude/skills/{name}"


def select_ca_cert(egress_plan: EgressPlan) -> tuple[Path, str]:
    """Return the egress MITM CA cert path and label for provision_ca.

    Launch always mints the CA and re-binds the host path into the
    egress_plan before provision runs, so an empty/missing path here
    means launch's bringup is broken — fatal."""
    cert = egress_plan.mitmproxy_ca_cert_only_host_path
    if cert == Path() or not cert.is_file():
        die(
            f"egress CA cert missing at {cert or '(empty)'}; "
            f"launch must have called egress_tls_init and "
            f"re-bound the plan before provision"
        )
    return cert, "egress"


def log_ca_fingerprint(cert_host_path: Path, label: str) -> None:
    """Compute the cert's SHA-256 fingerprint over its DER bytes
    (stdlib `ssl` + `hashlib`) and log it once to stderr — the
    standard fingerprint form. Only ever touches the public cert;
    the private key stays on the host under the stage dir until
    teardown."""
    der = ssl.PEM_cert_to_DER_cert(cert_host_path.read_text())
    fingerprint = hashlib.sha256(der).hexdigest()
    info(f"{label} ca fingerprint: sha256:{fingerprint[:32]}...")
