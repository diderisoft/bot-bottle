"""Neutral supervise vocabulary — importable by every tier.

The supervise plane spans the control plane (orchestrator) and the data plane
(gateway `supervise_server`): the gateway proposes/polls over RPC, the
orchestrator queues and applies operator decisions. Both sides must agree on
the wire types and constants, so those live here — a neutral package neither
tier's private code, importable by `bot_bottle.gateway` **and**
`bot_bottle.orchestrator` without either depending on the other.

  * `types` — the `Proposal`/`Response`/`AuditEntry` dataclasses, the tool /
    status / poll-status constants, and the shared daemon constants.
  * `plan`  — `SupervisePlan`, the launch-time staging DTO.

The orchestrator-only half (queue I/O, diff rendering, the `Supervisor`
lifecycle) lives under `bot_bottle.orchestrator.supervisor`.
"""
