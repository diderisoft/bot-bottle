"""Host-side helper for egress sidecar inspection (issue #198).

`_merge_single_route`, `add_route`, and `apply_routes_change` were
removed when the egress-block MCP tool was dropped. The remaining
helpers support runtime inspection and validation of the routes file
without modifying it at runtime.
"""

from __future__ import annotations

import subprocess

from ...egress import EGRESS_ROUTES_IN_CONTAINER
from ...egress_addon_core import load_routes
from .sidecar_bundle import sidecar_bundle_container_name


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


__all__ = [
    "EgressApplyError",
    "fetch_current_routes",
    "validate_routes_content",
]
