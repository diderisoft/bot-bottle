"""DockerBottlePlan — concrete subclass of BottlePlan.

Carries the Docker-specific resolved fields produced by
DockerBottleBackend.prepare. The launch step consumes it without
further resolution; preflight rendering is inherited from BottlePlan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import BottlePlan


@dataclass(frozen=True)
class DockerBottlePlan(BottlePlan):
    """Docker-specific resolved fields produced by
    DockerBottleBackend.prepare."""

    # Run the agent under gVisor's `runsc` runtime.
    use_runsc: bool = field(kw_only=True)
