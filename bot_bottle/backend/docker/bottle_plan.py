"""`DockerBottlePlan`: the docker backend's resolved launch plan."""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import BottlePlan


@dataclass(frozen=True)
class DockerBottlePlan(BottlePlan):
    """Docker-specific resolved fields."""

    # Run the agent under gVisor's `runsc`.
    use_runsc: bool = field(kw_only=True)
