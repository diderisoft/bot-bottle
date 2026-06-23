"""Egress apply for the smolmachines backend.

The smolmachines sidecar bundle runs as a host-side Docker container,
so egress signalling is identical to the docker backend.
"""

from __future__ import annotations

from ..docker.egress_apply import (  # noqa: F401
    EgressApplyError,
    apply_routes_change,
    fetch_current_routes,
    validate_routes_content,
)

__all__ = [
    "EgressApplyError",
    "apply_routes_change",
    "fetch_current_routes",
    "validate_routes_content",
]
