"""Host-side helper to apply a routes.yaml change to a running
egress sidecar (PRD 0014 retargeted by PRD 0017 chunk 3).

Used by the supervise dashboard when the operator approves an
egress-block proposal (or runs the operator-initiated
`routes edit <bottle>` verb). Fetches the current routes.yaml via
`docker exec cat`, validates the new content, writes it into the
sidecar via `docker cp`, then `docker kill --signal HUP` to make
the addon reload without dropping connections.

Also mirrors the new route hosts into pipelock's hostname allowlist
so the downstream leg lets them through — egress enforces
the path-aware allowlist on the agent leg, pipelock enforces the
hostname allowlist + DLP body scan on the upstream leg, and a
host added to one must be in the other or the request 403s
somewhere along the chain.

Raises EgressApplyError on any failure — the dashboard
surfaces the message and keeps the proposal pending so the
operator can retry.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ...egress import EGRESS_ROUTES_IN_CONTAINER
from ...egress_addon_core import load_routes
from ...yaml_subset import YamlSubsetError, parse_yaml_subset
from .bottle_state import egress_state_dir
from .egress import egress_container_name
from .pipelock_apply import (
    PipelockApplyError,
    apply_allowlist_change,
    fetch_current_allowlist,
    parse_allowlist_content,
    render_allowlist_content,
)


def _render_routes_payload(routes_list: list[dict[str, object]]) -> str:
    """Render a list-of-dicts routes payload as YAML matching the
    shape `egress_render_routes` produces. The apply path
    round-trips current routes.yaml through this so the file the
    sidecar sees stays in the YAML format the addon expects."""
    if not routes_list:
        return "routes: []\n"
    lines: list[str] = ["routes:"]
    for entry in routes_list:
        host = str(entry.get("host", ""))
        lines.append(f'  - host: "{host}"')
        auth_scheme = entry.get("auth_scheme")
        token_env = entry.get("token_env")
        if auth_scheme and token_env:
            lines.append(f'    auth_scheme: "{auth_scheme}"')
            lines.append(f'    token_env: "{token_env}"')
        paths = entry.get("path_allowlist") or []
        if paths:
            lines.append("    path_allowlist:")
            for p in paths:
                lines.append(f'      - "{p}"')
    return "\n".join(lines) + "\n"


def _egress_routes_host_path(slug: str) -> Path:
    """The bind-mount source for the egress sidecar's routes.yaml.
    Must match what egress.prepare wrote at chunk-2 paths."""
    return egress_state_dir(slug) / "egress_routes.yaml"


class EgressApplyError(RuntimeError):
    """Raised when fetch / apply fails. Caller renders to the
    operator; does not crash the dashboard."""


def fetch_current_routes(slug: str) -> str:
    """Read the live routes.yaml from the running egress sidecar
    for `slug`. Returns the file content as a string. Raises
    EgressApplyError if the sidecar isn't reachable or the read
    fails."""
    container = egress_container_name(slug)
    r = subprocess.run(
        ["docker", "exec", container, "cat", EGRESS_ROUTES_IN_CONTAINER],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise EgressApplyError(
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
        raise EgressApplyError(
            f"proposed routes.yaml is not valid: {e}"
        ) from e


def _hosts_in_routes(content: str) -> list[str]:
    """Extract the host list from a routes.yaml content string.
    Uses the addon's own parser so any host the addon will match on
    also lands in pipelock's allowlist. Returns sorted+deduped."""
    try:
        routes = load_routes(content)
    except ValueError as e:
        raise EgressApplyError(
            f"proposed routes.yaml is not valid: {e}"
        ) from e
    return sorted({r.host for r in routes if r.host})


# Pipelock's allowlist parser accepts only literal hostnames:
# `[A-Za-z0-9_.-]+`. Anything else (wildcards, IPv6 literals,
# stray characters) is silently dropped from the mirror so the
# pipelock apply doesn't fail parse before the new yaml is even
# written. The dropped hosts stay on egress's route table —
# but the addon does exact-host match only, so they'll never
# match anything either. (Wildcard host matching was removed —
# see `match_route` in egress_addon_core for the rationale.)
_PIPELOCK_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _pipelock_safe_hosts(hosts: list[str]) -> list[str]:
    """Drop any host pipelock's allowlist parser would reject.
    Order preserved."""
    return [h for h in hosts if _PIPELOCK_HOST_RE.match(h)]


def _mirror_hosts_to_pipelock(slug: str, hosts: list[str]) -> None:
    """Ensure every pipelock-compatible `hosts` entry is on
    pipelock's allowlist. Fetches pipelock's current allowlist,
    merges, re-applies. Hosts pipelock can't represent (wildcards,
    etc.) are silently skipped — they stay live on egress
    but aren't enforced at pipelock. No-op if every host is already
    present (apply still restarts pipelock if any host is new).
    Raises EgressApplyError on pipelock failures so the
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
        # Mirror runs BEFORE the egress write, so egress
        # is unchanged on this failure path. Report it as a
        # pipelock-side problem so the operator looks in the right
        # place; their `pipelock edit` flow can repair manually.
        raise EgressApplyError(
            f"pipelock allowlist mirror failed (egress NOT "
            f"updated): {e}. Fix pipelock's allowlist manually with "
            f"`pipelock edit <bottle>` then retry the proposal."
        ) from e


def apply_routes_change(slug: str, new_content: str) -> tuple[str, str]:
    """Apply `new_content` to the egress sidecar for `slug`:
      1. Fetch current routes.yaml (for the before-diff).
      2. Validate the new content via the addon's own parser.
      3. Mirror the route hosts onto pipelock's allowlist (so the
         downstream hostname gate lets them through).
      4. Write to a temp file, `docker cp` into the egress
         sidecar.
      5. `docker kill --signal HUP` so the addon reloads.

    Order matters: pipelock first, then egress. If the
    pipelock step fails, egress hasn't been touched and the
    old routes stay live. If the egress step fails after
    pipelock succeeded, pipelock has the host in its allowlist but
    egress doesn't enforce it yet — harmless extra-permissive
    state at pipelock, and a re-approval will land the egress
    side.

    Returns (before, after) where `after` == `new_content`. Raises
    EgressApplyError on any step."""
    container = egress_container_name(slug)
    before = fetch_current_routes(slug)
    validate_routes_content(new_content)

    # Pipelock mirror first — if it fails, egress stays intact
    # and the operator gets a clear error about the half-state.
    _mirror_hosts_to_pipelock(slug, _hosts_in_routes(new_content))

    # routes.yaml is bind-mounted into the egress container as a
    # SINGLE FILE. Docker single-file bind mounts pin the source
    # inode at mount time; write-temp-then-rename swaps the inode
    # on the host, which leaves the container's mount pointing at
    # the now-orphaned old inode (so the SIGHUP'd reload re-reads
    # unchanged content). Write in-place instead. Lose file-level
    # atomicity, but the apply path issues SIGHUP only AFTER the
    # write returns, and the addon's `load_routes` raises
    # `ValueError` on a partial read and keeps the previous
    # in-memory routes — so a SIGHUP that hypothetically raced an
    # in-flight write is non-disruptive.
    target = _egress_routes_host_path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_content)
    # mitmproxy in the container reads through the bind mount as
    # uid 1000; the host file has to be world-readable for that
    # read to succeed (parent dir at 0o700 still restricts who
    # can reach the file on the host). Routes content is not
    # secret — tokens live in the container's environ — so 0o644
    # is the right trade-off.
    target.chmod(0o644)
    sig = subprocess.run(
        ["docker", "kill", "--signal", "HUP", container],
        capture_output=True, text=True, check=False,
    )
    if sig.returncode != 0:
        raise EgressApplyError(
            f"failed to SIGHUP {container}: "
            f"{(sig.stderr or '').strip()}"
        )

    return before, new_content


def _merge_single_route(
    current_yaml: str, new_route: dict[str, object],
) -> str:
    """Merge a single proposed route into the current routes.yaml
    content, returning the merged YAML string.

    Behavior:
      - If `new_route['host']` is NOT in the current routes →
        append the route.
      - If the host IS already present → union the path_allowlist
        entries (proposed ∪ existing). The existing `auth_scheme`
        and `token_env` are preserved — agent-proposed auth changes
        on an existing host are ignored, matching the tool's
        documented semantics.

    Round-trips the file through `yaml_subset` (the same parser
    the addon uses), so the merged output is in the YAML format
    the sidecar reads. Token VALUES never appear here; the routes
    file carries only env-var slot NAMES."""
    try:
        cfg = parse_yaml_subset(current_yaml)
    except YamlSubsetError as e:
        raise EgressApplyError(
            f"current routes.yaml is not valid YAML: {e}"
        ) from e
    routes = cfg.get("routes")
    if not isinstance(routes, list):
        raise EgressApplyError(
            "current routes.yaml: 'routes' is not a list"
        )

    new_host = str(new_route.get("host", "")).lower()
    if not new_host:
        raise EgressApplyError(
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
            entry["token_env"] = f"EGRESS_TOKEN_{next_idx}"
            # NOTE: the addon reads token VALUES from its container's
            # environ keyed by token_env. A newly-added auth route at
            # runtime points at a slot that has no env value → the
            # addon will 403 with "token env unset" until the operator
            # arranges for the value to land in the container's env.
            # Recording this here so the operator-facing diff carries
            # the slot name they'll need to provision.
        routes.append(entry)

    return _render_routes_payload(routes)


def add_route(slug: str, proposed_route_json: str) -> tuple[str, str]:
    """Apply a single-route addition to the egress. Parses the
    agent's proposed route, fetches the current routes file, merges,
    and applies via `apply_routes_change`. Returns (before, after)
    full-file content for the audit log."""
    try:
        proposed = json.loads(proposed_route_json)
    except json.JSONDecodeError as e:
        raise EgressApplyError(
            f"proposed route is not valid JSON: {e}"
        ) from e
    if not isinstance(proposed, dict):
        raise EgressApplyError(
            "proposed route must be a JSON object"
        )
    current = fetch_current_routes(slug)
    merged = _merge_single_route(current, proposed)
    return apply_routes_change(slug, merged)


__all__ = [
    "EgressApplyError",
    "add_route",
    "apply_routes_change",
    "fetch_current_routes",
    "validate_routes_content",
]
