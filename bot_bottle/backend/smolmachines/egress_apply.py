"""Egress apply for the smolmachines backend.

The smolmachines sidecar bundle runs as a host-side Docker container,
so egress signalling is identical to the docker backend.
"""

from __future__ import annotations

from ..docker.egress_apply import (  # noqa: F401
    DockerEgressApplicator,
    EgressApplyError,
    applicator,
    fetch_current_routes,
)

__all__ = [
    "DockerEgressApplicator",
    "EgressApplyError",
    "applicator",
    "fetch_current_routes",
]
