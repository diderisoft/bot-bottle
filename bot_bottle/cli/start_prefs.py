"""What the guided `bot-bottle start` remembers for you: the agent (harness)
you last ran, the host's remembered agent (`agent_prefs`, kept by the host
controller). With none, or no host controller to ask, nothing is remembered
and `start` asks."""

from __future__ import annotations

from ..control.hostctl.runner import HostControlError
from . import host_state_client


def remembered_agent() -> str:
    """The remembered agent, or ""."""
    try:
        return host_state_client.agent_prefs().default
    except HostControlError:
        return ""


def remember_agent(agent: str) -> None:
    """Make `agent` the remembered one; never fails the launch."""
    try:
        host_state_client.remember_agent(agent)
    except HostControlError:
        pass


__all__ = ["remember_agent", "remembered_agent"]
