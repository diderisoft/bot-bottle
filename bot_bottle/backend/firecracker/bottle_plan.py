"""Plan type for the Firecracker backend."""

from __future__ import annotations

from dataclasses import dataclass

from .. import BottlePlan


@dataclass(frozen=True)
class FirecrackerBottlePlan(BottlePlan):
    # The agent's egress proxy URL, carrying the identity token as proxy
    # credentials. Stamped by launch (empty at prepare time).
    agent_proxy_url: str = ""
