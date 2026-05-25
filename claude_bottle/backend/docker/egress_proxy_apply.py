"""Host-side helper to apply a routes.yaml change to a running
egress-proxy sidecar (PRD 0014 retargeted by PRD 0017 chunk 3).

Used by the supervise dashboard when the operator approves an
egress-proxy-block proposal (or runs the operator-initiated
`routes edit <bottle>` verb). Fetches the current routes.yaml via
`docker exec cat`, validates the new content, writes it into the
sidecar via `docker cp`, then `docker kill --signal HUP` to make
the addon reload without dropping connections.

Also mirrors the new route hosts into pipelock's hostname allowlist
so the downstream leg lets them through — egress-proxy enforces
the path-aware allowlist on the agent leg, pipelock enforces the
hostname allowlist + DLP body scan on the upstream leg, and a
host added to one must be in the other or the request 403s
somewhere along the chain.

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
from .pipelock_apply import (
    PipelockApplyError,
    apply_allowlist_change,
    fetch_current_allowlist,
    parse_allowlist_content,
    render_allowlist_content,
)


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


def _hosts_in_routes(content: str) -> list[str]:
    """Extract the host list from a routes.yaml content string.
    Uses the addon's own parser so any host the addon will match on
    also lands in pipelock's allowlist. Returns sorted+deduped."""
    try:
        routes = load_routes(content)
    except ValueError as e:
        raise EgressProxyApplyError(
            f"proposed routes.yaml is not valid: {e}"
        ) from e
    return sorted({r.host for r in routes if r.host})


def _mirror_hosts_to_pipelock(slug: str, hosts: list[str]) -> None:
    """Ensure every `hosts` entry is on pipelock's allowlist. Fetches
    pipelock's current allowlist, merges, re-applies. No-op if every
    host is already present (apply still restarts pipelock if any
    host is new). Raises EgressProxyApplyError on pipelock failures
    so the caller's diff/audit reflects the half-state."""
    try:
        current = fetch_current_allowlist(slug)
        existing = parse_allowlist_content(current)
        merged = sorted(set(existing) | set(hosts))
        if merged == sorted(existing):
            return  # nothing to add
        apply_allowlist_change(slug, render_allowlist_content(merged))
    except PipelockApplyError as e:
        raise EgressProxyApplyError(
            f"egress-proxy routes updated but pipelock allowlist "
            f"mirror failed: {e}. The request will 403 at pipelock "
            f"until pipelock's allowlist is refreshed; retry the "
            f"proposal or edit pipelock's allowlist by hand."
        ) from e


def apply_routes_change(slug: str, new_content: str) -> tuple[str, str]:
    """Apply `new_content` to the egress-proxy sidecar for `slug`:
      1. Fetch current routes.yaml (for the before-diff).
      2. Validate the new content via the addon's own parser.
      3. Mirror the route hosts onto pipelock's allowlist (so the
         downstream hostname gate lets them through).
      4. Write to a temp file, `docker cp` into the egress-proxy
         sidecar.
      5. `docker kill --signal HUP` so the addon reloads.

    Order matters: pipelock first, then egress-proxy. If the
    pipelock step fails, egress-proxy hasn't been touched and the
    old routes stay live. If the egress-proxy step fails after
    pipelock succeeded, pipelock has the host in its allowlist but
    egress-proxy doesn't enforce it yet — harmless extra-permissive
    state at pipelock, and a re-approval will land the egress-proxy
    side.

    Returns (before, after) where `after` == `new_content`. Raises
    EgressProxyApplyError on any step."""
    container = egress_proxy_container_name(slug)
    before = fetch_current_routes(slug)
    validate_routes_content(new_content)

    # Pipelock mirror first — if it fails, egress-proxy stays intact
    # and the operator gets a clear error about the half-state.
    _mirror_hosts_to_pipelock(slug, _hosts_in_routes(new_content))

    fd, tmp_path = tempfile.mkstemp(prefix="cb-routes.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_content)
        # mkstemp creates the file with mode 0600. `docker cp`
        # preserves mode + host uid into the container, so without
        # chmod the file lands as 0600 owned by the host user's uid,
        # which inside the container is not mitmproxy (uid 1000) —
        # the addon's reload then fails with PermissionError on the
        # SIGHUP-triggered re-read and the old routes table stays in
        # memory. Bump to 0644 so mitmproxy can read it post-cp;
        # the host stage_dir doesn't apply to this tmp file but the
        # content isn't secret (no tokens — those live in the
        # container's environ), so 0644 in /tmp is fine.
        os.chmod(tmp_path, 0o644)
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
