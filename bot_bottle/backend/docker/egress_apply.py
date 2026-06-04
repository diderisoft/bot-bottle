"""Host-side helper to apply a routes.yaml change to a running
egress sidecar (PRD 0014 retargeted by PRD 0017 chunk 3).

Used by the supervise dashboard when the operator approves an
egress-block proposal (or runs the operator-initiated
`routes edit <bottle>` verb). Fetches the current routes.yaml via
`docker exec cat`, validates the new content, writes it into the
sidecar via `docker cp`, then `docker kill --signal HUP` to make
the addon reload without dropping connections.

Raises EgressApplyError on any failure — the dashboard
surfaces the message and keeps the proposal pending so the
operator can retry.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

from ...egress import EGRESS_ROUTES_IN_CONTAINER
from ...egress_addon_core import load_routes
from ...yaml_subset import YamlSubsetError, parse_yaml_subset
from .bottle_state import egress_state_dir
from .sidecar_bundle import sidecar_bundle_container_name


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
        paths_obj = entry.get("path_allowlist")
        paths = cast(list[str], paths_obj) if isinstance(paths_obj, list) else []
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
    container = sidecar_bundle_container_name(slug)
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


def apply_routes_change(slug: str, new_content: str) -> tuple[str, str]:
    """Apply `new_content` to the egress sidecar for `slug`:
      1. Fetch current routes.yaml (for the before-diff).
      2. Validate the new content via the addon's own parser.
      3. Write to the bind-mount source path.
      4. `docker kill --signal HUP` so the addon reloads.

    Returns (before, after) where `after` == `new_content`. Raises
    EgressApplyError on any step."""
    container = sidecar_bundle_container_name(slug)
    before = fetch_current_routes(slug)
    validate_routes_content(new_content)

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
    routes_typed = cast(list[object], routes)

    new_host = str(new_route.get("host", "")).lower()
    if not new_host:
        raise EgressApplyError(
            "proposed route is missing 'host'"
        )

    proposed_paths_obj = new_route.get("path_allowlist")
    proposed_paths = cast(list[str], proposed_paths_obj) if isinstance(proposed_paths_obj, list) else []

    # Look for an existing entry with the same host (case-insensitive).
    for entry in routes_typed:
        if not isinstance(entry, dict):
            continue
        entry_typed = cast(dict[str, object], entry)
        if str(entry_typed.get("host", "")).lower() == new_host:
            # Merge path_allowlist: union proposed + existing, ordered
            # by first-seen so existing paths stay in original order.
            existing_paths_obj = entry_typed.get("path_allowlist")
            existing_paths = cast(list[str], existing_paths_obj) if isinstance(existing_paths_obj, list) else []
            seen = {p: None for p in existing_paths}
            for p in proposed_paths:
                seen.setdefault(p, None)
            merged_paths = list(seen.keys())
            if merged_paths:
                entry_typed["path_allowlist"] = merged_paths
            # Preserve existing auth — tool description says agent-
            # proposed auth on an existing host is ignored.
            break
    else:
        # Host not present; build a new route entry from the
        # proposed fields. Need to assign a token_env slot if
        # `auth` was proposed (otherwise the addon's parser rejects
        # a half-set auth pair). Slots: count existing slots, pick
        # the next free index.
        entry_typed: dict[str, object] = {"host": new_route.get("host")}  # type: ignore
        if proposed_paths:
            entry_typed["path_allowlist"] = proposed_paths
        auth = new_route.get("auth")
        if isinstance(auth, dict) and auth.get("scheme") and auth.get("token_ref"):  # type: ignore
            auth_typed = cast(dict[str, object], auth)
            existing_slots = sorted({
                str(r_entry.get("token_env", ""))
                for r_entry_obj in routes_typed
                if isinstance(r_entry_obj, dict)
                for r_entry in [cast(dict[str, object], r_entry_obj)]
                if r_entry.get("token_env")
            })
            next_idx = len(existing_slots)
            entry_typed["auth_scheme"] = str(cast(object, auth_typed.get("scheme")))
            entry_typed["token_env"] = f"EGRESS_TOKEN_{next_idx}"
        routes_typed.append(entry_typed)

    return _render_routes_payload(cast(list[dict[str, object]], routes_typed))


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
