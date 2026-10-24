"""Plan type for the Firecracker backend."""

from __future__ import annotations

from dataclasses import dataclass

from .. import BottlePlan


@dataclass(frozen=True)
class FirecrackerBottlePlan(BottlePlan):
    # The agent's proxy URL with the identity token as credentials. Set by
    # launch.
    agent_proxy_url: str = ""
