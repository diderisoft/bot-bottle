"""Host-side helper to apply a routes.yaml change to a running
egress sidecar (PRD 0014 retargeted by PRD 0017 chunk 3, PRD 0053).

Used by the supervise dashboard when the operator approves an
egress-block proposal. Fetches current routes.yaml, validates,
writes into the sidecar, then SIGHUPs to reload.
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
    shape `egress_render_routes` produces."""
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
        matches_obj = entry.get("matches")
        if isinstance(matches_obj, list) and matches_obj:
            lines.append("    matches:")
            for match_entry in matches_obj:
                me = cast(dict[str, object], match_entry)
                first_key = True
                if "paths" in me:
                    lines.append("      - paths:")
                    first_key = False
                    for pd in cast(list[dict[str, str]], me["paths"]):
                        if "type" in pd:
                            lines.append(f'          - type: "{pd["type"]}"')
                            lines.append(f'            value: "{pd["value"]}"')
                        else:
                            lines.append(f'          - value: "{pd["value"]}"')
                if "methods" in me:
                    methods_str = ", ".join(
                        f'"{m}"' for m in cast(list[str], me["methods"])
                    )
                    prefix = "      - " if first_key else "        "
                    lines.append(f'{prefix}methods: [{methods_str}]')
                    first_key = False
                if first_key:
                    lines.append("      - {}")
    return "\n".join(lines) + "\n"


def _egress_routes_host_path(slug: str) -> Path:
    return egress_state_dir(slug) / "egress_routes.yaml"


class EgressApplyError(RuntimeError):
    pass


def fetch_current_routes(slug: str) -> str:
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
    try:
        load_routes(content)
    except ValueError as e:
        raise EgressApplyError(
            f"proposed routes.yaml is not valid: {e}"
        ) from e


def apply_routes_change(slug: str, new_content: str) -> tuple[str, str]:
    container = sidecar_bundle_container_name(slug)
    before = fetch_current_routes(slug)
    validate_routes_content(new_content)

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
    """Merge a single proposed route into the current routes.yaml.

    - Host absent → append the route.
    - Host present → union the match paths (proposed ∪ existing).
      Auth is preserved from existing route.
    """
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

    # Build proposed matches from the input
    proposed_matches = new_route.get("matches")
    if proposed_matches is None:
        # Accept legacy path_allowlist from agent proposals and convert
        proposed_paths = new_route.get("path_allowlist")
        if isinstance(proposed_paths, list) and proposed_paths:
            proposed_matches = [{"paths": [{"value": p} for p in proposed_paths]}]

    for entry in routes_typed:
        if not isinstance(entry, dict):
            continue
        entry_typed = cast(dict[str, object], entry)
        if str(entry_typed.get("host", "")).lower() == new_host:
            # Merge matches: union path values from proposed into existing
            if isinstance(proposed_matches, list) and proposed_matches:
                existing_matches = entry_typed.get("matches")
                if not isinstance(existing_matches, list):
                    existing_matches = []
                # Simple merge: collect all existing path values, add new ones
                existing_paths: set[str] = set()
                for me in existing_matches:
                    me_typed = cast(dict[str, object], me) if isinstance(me, dict) else {}
                    paths = me_typed.get("paths")
                    if isinstance(paths, list):
                        for p in paths:
                            p_typed = cast(dict[str, object], p) if isinstance(p, dict) else {}
                            val = p_typed.get("value")
                            if isinstance(val, str):
                                existing_paths.add(val)
                new_paths: list[str] = []
                for me in proposed_matches:
                    me_typed = cast(dict[str, object], me) if isinstance(me, dict) else {}
                    paths = me_typed.get("paths")
                    if isinstance(paths, list):
                        for p in paths:
                            p_typed = cast(dict[str, object], p) if isinstance(p, dict) else {}
                            val = p_typed.get("value")
                            if isinstance(val, str) and val not in existing_paths:
                                new_paths.append(val)
                                existing_paths.add(val)
                if new_paths:
                    if not isinstance(existing_matches, list):
                        existing_matches = []
                    existing_matches.append(
                        {"paths": [{"value": p} for p in new_paths]}
                    )
                    entry_typed["matches"] = existing_matches
            break
    else:
        entry_typed: dict[str, object] = {"host": new_route.get("host")}  # type: ignore
        if isinstance(proposed_matches, list) and proposed_matches:
            entry_typed["matches"] = proposed_matches
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
