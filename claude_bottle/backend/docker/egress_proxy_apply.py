"""Host-side helper to apply a routes.yaml change to a running
egress-proxy sidecar (PRD 0014 retargeted by PRD 0017 chunk 3).

Used by the supervise dashboard when the operator approves an
egress-proxy-block proposal (or runs the operator-initiated
`routes edit <bottle>` verb). Fetches the current routes.yaml via
`docker exec cat`, validates the new content, writes it into the
sidecar via `docker cp`, then `docker kill --signal HUP` to make
the addon reload without dropping connections.

Raises EgressProxyApplyError on any failure — the dashboard
surfaces the message and keeps the proposal pending so the
operator can retry.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ...egress_proxy import EGRESS_PROXY_ROUTES_IN_CONTAINER
from ...egress_proxy_addon_core import load_routes
from .egress_proxy import egress_proxy_container_name


class EgressProxyApplyError(RuntimeError):
    """Raised when fetch / apply fails. Caller renders to the
    operator; does not crash the dashboard."""


def fetch_current_routes(slug: str) -> str:
    """Read the live routes.yaml from the running egress-proxy sidecar
    for `slug`. Returns the file content as a string. Raises
    EgressProxyApplyError if the sidecar isn't reachable or the read
    fails."""
    container = egress_proxy_container_name(slug)
    r = subprocess.run(
        ["docker", "exec", container, "cat", EGRESS_PROXY_ROUTES_IN_CONTAINER],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise EgressProxyApplyError(
            f"could not read routes.yaml from {container}: "
            f"{(r.stderr or '').strip() or 'container not running?'}"
        )
    return r.stdout


def validate_routes_content(content: str) -> None:
    """Syntactic check before SIGHUP — the addon's reload also
    validates, but failing here keeps the old routes live and gives
    the operator a clearer error than the addon's stderr line."""
    try:
        load_routes(content)
    except ValueError as e:
        raise EgressProxyApplyError(
            f"proposed routes.yaml is not valid: {e}"
        ) from e


def apply_routes_change(slug: str, new_content: str) -> tuple[str, str]:
    """Apply `new_content` to the egress-proxy sidecar for `slug`:
      1. Fetch current routes.yaml (for the before-diff).
      2. Validate the new content via the addon's own parser.
      3. Write to a temp file, `docker cp` into the sidecar.
      4. `docker kill --signal HUP` so the addon reloads.

    Returns (before, after) where `after` == `new_content`. Raises
    EgressProxyApplyError on any step; the existing routes in the
    sidecar are unchanged if the failure is before docker cp, and
    are reverted in spirit if SIGHUP fails (cp landed but reload
    didn't fire — caller's next attempt will SIGHUP again)."""
    container = egress_proxy_container_name(slug)
    before = fetch_current_routes(slug)
    validate_routes_content(new_content)

    fd, tmp_path = tempfile.mkstemp(prefix="cb-routes.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_content)
        cp = subprocess.run(
            ["docker", "cp", tmp_path,
             f"{container}:{EGRESS_PROXY_ROUTES_IN_CONTAINER}"],
            capture_output=True, text=True, check=False,
        )
        if cp.returncode != 0:
            raise EgressProxyApplyError(
                f"failed to copy routes.yaml into {container}: "
                f"{(cp.stderr or '').strip()}"
            )
        sig = subprocess.run(
            ["docker", "kill", "--signal", "HUP", container],
            capture_output=True, text=True, check=False,
        )
        if sig.returncode != 0:
            raise EgressProxyApplyError(
                f"failed to SIGHUP {container}: "
                f"{(sig.stderr or '').strip()}"
            )
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    return before, new_content


__all__ = [
    "EgressProxyApplyError",
    "apply_routes_change",
    "fetch_current_routes",
    "validate_routes_content",
]
