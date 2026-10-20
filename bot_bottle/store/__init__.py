"""SQLite-backed persistence base for bot-bottle.

Concrete stores that aren't owned by a single service — `ConfigStore` and
`AuditStore` — built on `bot_bottle.lib.db_store.DbStore`.

Service-owned stores live under their service: the supervise `queue_store`, the
`secret_store`, the orchestrator `config_store`, and the orchestrator
`store_manager` are under `bot_bottle.orchestrator.store`.

Callers import the concrete module they need directly, e.g.
`from bot_bottle.store.audit_store import AuditStore`.
"""
