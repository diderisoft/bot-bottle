"""The agent <-> infra protocol version.

Every published image records the protocol it was built for, and a host refuses
an agent image whose protocol differs from its infra images' or its own. It is
not the release version: bump it only when an older agent image would break
under newer infra or the reverse (guest init, the orchestrator API,
identity/token delivery, the gateway's agent-facing ports and env).
"""

from __future__ import annotations

PROTOCOL_VERSION = 1

__all__ = ["PROTOCOL_VERSION"]
