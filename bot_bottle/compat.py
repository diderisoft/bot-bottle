"""The agent <-> infra protocol version.

An agent image boots against the infra it was published with: the guest init,
the orchestrator's control-plane contract, and what the gateway expects of an
agent. `PROTOCOL_VERSION` names that contract. Every published image records the
version it was built for (`protocol` in `published-artifacts.json`), and a host
refuses an agent image whose protocol differs from its infra images' or from its
own.

This is deliberately *not* the release version. Releases move for any reason —
a harness bump, a docs fix, republishing one image — while the protocol moves
only when the contract does, so a partial republish at a new version stays
compatible as long as the protocol is unchanged.

Bump it when a change would make an older agent image unbootable or
misbehaving under newer infra (or the reverse): guest-init's interface, the
orchestrator API agents and the gateway use, identity/token delivery, or the
gateway's agent-facing ports and environment.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1

__all__ = ["PROTOCOL_VERSION"]
