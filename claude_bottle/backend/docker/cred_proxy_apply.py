"""Host-side helper to apply a routes.json change to a running
cred-proxy sidecar (PRD 0014).

Used by the supervise dashboard when the operator approves a
cred-proxy-block proposal (or runs the operator-initiated `routes
edit <bottle>` verb). Fetches the current routes.json via `docker
exec cat`, validates the new JSON, writes it into the sidecar via
`docker cp`, then `docker kill --signal HUP` to make the in-sidecar
SIGHUP handler (PRD 0014 Phase 1) reload without dropping
connections.

Raises CredProxyApplyError on any failure — the dashboard surfaces
the message and keeps the proposal pending so the operator can
retry.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .cred_proxy import (
    CRED_PROXY_ROUTES_IN_CONTAINER,
    cred_proxy_container_name,
)


class CredProxyApplyError(RuntimeError):
    """Raised when fetch / apply fails. Caller renders to the
    operator; does not crash the dashboard."""


def fetch_current_routes(slug: str) -> str:
    """Read the live routes.json from the running cred-proxy sidecar
    for `slug`. Returns the file content as a string. Raises
    CredProxyApplyError if the sidecar isn't reachable or the read
    fails."""
    container = cred_proxy_container_name(slug)
    r = subprocess.run(
        ["docker", "exec", container, "cat", CRED_PROXY_ROUTES_IN_CONTAINER],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise CredProxyApplyError(
            f"could not read routes.json from {container}: "
            f"{(r.stderr or '').strip() or 'container not running?'}"
        )
    return r.stdout


def validate_routes_json(content: str) -> None:
    """Syntactic check before SIGHUP — the sidecar's reload also
    validates, but failing here keeps the old routes live and gives
    the operator a clearer error than 'reload failed' in the
    sidecar logs."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise CredProxyApplyError(
            f"proposed routes.json is not valid JSON: {e}"
        ) from e
    if not isinstance(parsed, dict) or not isinstance(parsed.get("routes"), list):
        raise CredProxyApplyError(
            "proposed routes.json must be an object with a 'routes' array"
        )


def apply_routes_change(slug: str, new_content: str) -> tuple[str, str]:
    """Apply `new_content` to the cred-proxy sidecar for `slug`:
      1. Fetch current routes.json (for the before-diff).
      2. Validate the new JSON.
      3. Write to a temp file, `docker cp` into the sidecar.
      4. `docker kill --signal HUP` so cred-proxy reloads.

    Returns (before, after) where `after` == `new_content`. Raises
    CredProxyApplyError on any step; the existing routes in the
    sidecar are unchanged if the failure is before docker cp, and
    are reverted in spirit if SIGHUP fails (cp landed but reload
    didn't fire — caller's next attempt will SIGHUP again)."""
    container = cred_proxy_container_name(slug)
    before = fetch_current_routes(slug)
    validate_routes_json(new_content)

    fd, tmp_path = tempfile.mkstemp(prefix="cb-routes.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_content)
        cp = subprocess.run(
            ["docker", "cp", tmp_path, f"{container}:{CRED_PROXY_ROUTES_IN_CONTAINER}"],
            capture_output=True, text=True, check=False,
        )
        if cp.returncode != 0:
            raise CredProxyApplyError(
                f"failed to copy routes.json into {container}: "
                f"{(cp.stderr or '').strip()}"
            )
        sig = subprocess.run(
            ["docker", "kill", "--signal", "HUP", container],
            capture_output=True, text=True, check=False,
        )
        if sig.returncode != 0:
            raise CredProxyApplyError(
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
    "CredProxyApplyError",
    "apply_routes_change",
    "fetch_current_routes",
    "validate_routes_json",
]
