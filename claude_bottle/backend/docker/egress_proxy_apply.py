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

import json
import os
import re
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


# Pipelock's allowlist parser accepts only literal hostnames:
# `[A-Za-z0-9_.-]+`. Anything else (wildcards, IPv6 literals,
# stray characters) is silently dropped from the mirror so the
# pipelock apply doesn't fail parse before the new yaml is even
# written. The dropped hosts stay on egress-proxy's route table —
# but the addon does exact-host match only, so they'll never
# match anything either. (Wildcard host matching was removed —
# see `match_route` in egress_proxy_addon_core for the rationale.)
_PIPELOCK_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _pipelock_safe_hosts(hosts: list[str]) -> list[str]:
    """Drop any host pipelock's allowlist parser would reject.
    Order preserved."""
    return [h for h in hosts if _PIPELOCK_HOST_RE.match(h)]


def _mirror_hosts_to_pipelock(slug: str, hosts: list[str]) -> None:
    """Ensure every pipelock-compatible `hosts` entry is on
    pipelock's allowlist. Fetches pipelock's current allowlist,
    merges, re-applies. Hosts pipelock can't represent (wildcards,
    etc.) are silently skipped — they stay live on egress-proxy
    but aren't enforced at pipelock. No-op if every host is already
    present (apply still restarts pipelock if any host is new).
    Raises EgressProxyApplyError on pipelock failures so the
    caller's diff/audit reflects the half-state."""
    safe_hosts = _pipelock_safe_hosts(hosts)
    try:
        current = fetch_current_allowlist(slug)
        existing = parse_allowlist_content(current)
        merged = sorted(set(existing) | set(safe_hosts))
        if merged == sorted(existing):
            return  # nothing to add
        apply_allowlist_change(slug, render_allowlist_content(merged))
    except PipelockApplyError as e:
        # Mirror runs BEFORE the egress-proxy write, so egress-proxy
        # is unchanged on this failure path. Report it as a
        # pipelock-side problem so the operator looks in the right
        # place; their `pipelock edit` flow can repair manually.
        raise EgressProxyApplyError(
            f"pipelock allowlist mirror failed (egress-proxy NOT "
            f"updated): {e}. Fix pipelock's allowlist manually with "
            f"`pipelock edit <bottle>` then retry the proposal."
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


def _merge_single_route(
    current_yaml: str, new_route: dict[str, object],
) -> str:
    """Merge a single proposed route into the current routes.yaml
    content, returning the merged JSON-as-yaml string.

    Behavior:
      - If `new_route['host']` is NOT in the current routes →
        append the route.
      - If the host IS already present → union the path_allowlist
        entries (proposed ∪ existing). The existing `auth_scheme`
        and `token_env` are preserved — agent-proposed auth changes
        on an existing host are ignored, matching the tool's
        documented semantics.

    The supervisor renders the merged routes.yaml with the same
    JSON layout the addon expects (host + path_allowlist +
    auth_scheme + token_env). Token VALUES never appear here; the
    routes file carries only env-var slot NAMES."""
    try:
        cfg = json.loads(current_yaml)
    except json.JSONDecodeError as e:
        raise EgressProxyApplyError(
            f"current routes.yaml is not valid JSON: {e}"
        ) from e
    routes = cfg.get("routes")
    if not isinstance(routes, list):
        raise EgressProxyApplyError(
            "current routes.yaml: 'routes' is not a list"
        )

    new_host = str(new_route.get("host", "")).lower()
    if not new_host:
        raise EgressProxyApplyError(
            "proposed route is missing 'host'"
        )

    proposed_paths = list(new_route.get("path_allowlist") or [])

    # Look for an existing entry with the same host (case-insensitive).
    for entry in routes:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("host", "")).lower() == new_host:
            # Merge path_allowlist: union proposed + existing, ordered
            # by first-seen so existing paths stay in original order.
            existing_paths: list[str] = list(entry.get("path_allowlist") or [])
            seen = {p: None for p in existing_paths}
            for p in proposed_paths:
                seen.setdefault(p, None)
            merged_paths = list(seen.keys())
            if merged_paths:
                entry["path_allowlist"] = merged_paths
            # Preserve existing auth — tool description says agent-
            # proposed auth on an existing host is ignored.
            break
    else:
        # Host not present; build a new route entry from the
        # proposed fields. Need to assign a token_env slot if
        # `auth` was proposed (otherwise the addon's parser rejects
        # a half-set auth pair). Slots: count existing slots, pick
        # the next free index.
        entry = {"host": new_route["host"]}
        if proposed_paths:
            entry["path_allowlist"] = proposed_paths
        auth = new_route.get("auth")
        if isinstance(auth, dict) and auth.get("scheme") and auth.get("token_ref"):
            existing_slots = sorted({
                str(r.get("token_env"))
                for r in routes
                if isinstance(r, dict) and r.get("token_env")
            })
            next_idx = len(existing_slots)
            entry["auth_scheme"] = str(auth["scheme"])
            entry["token_env"] = f"EGRESS_PROXY_TOKEN_{next_idx}"
            # NOTE: the addon reads token VALUES from its container's
            # environ keyed by token_env. A newly-added auth route at
            # runtime points at a slot that has no env value → the
            # addon will 403 with "token env unset" until the operator
            # arranges for the value to land in the container's env.
            # Recording this here so the operator-facing diff carries
            # the slot name they'll need to provision.
        routes.append(entry)

    cfg["routes"] = routes
    return json.dumps(cfg, indent=2) + "\n"


def add_route(slug: str, proposed_route_json: str) -> tuple[str, str]:
    """Apply a single-route addition to the egress-proxy. Parses the
    agent's proposed route, fetches the current routes file, merges,
    and applies via `apply_routes_change`. Returns (before, after)
    full-file content for the audit log."""
    try:
        proposed = json.loads(proposed_route_json)
    except json.JSONDecodeError as e:
        raise EgressProxyApplyError(
            f"proposed route is not valid JSON: {e}"
        ) from e
    if not isinstance(proposed, dict):
        raise EgressProxyApplyError(
            "proposed route must be a JSON object"
        )
    current = fetch_current_routes(slug)
    merged = _merge_single_route(current, proposed)
    return apply_routes_change(slug, merged)


__all__ = [
    "EgressProxyApplyError",
    "add_route",
    "apply_routes_change",
    "fetch_current_routes",
    "validate_routes_content",
]
