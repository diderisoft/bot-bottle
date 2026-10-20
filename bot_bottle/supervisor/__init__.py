"""Host-side supervise pieces.

  * `plan` — `SupervisePlan`, the launch-time staging DTO.

The wire types both tiers share (`Proposal`/`Response`/`AuditEntry` and the
tool / status constants) are in `bot_bottle.contract.supervise`. The
orchestrator-only half (queue I/O, diff rendering, the `Supervisor` lifecycle)
lives under `bot_bottle.orchestrator.supervisor`.
"""
