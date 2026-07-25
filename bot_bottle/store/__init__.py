"""SQLite-backed persistence base for bot-bottle.

The shared store base: the `DbStore` class (versioned `migrations`) and the
concrete stores built on it that aren't owned by a single service —
`ConfigStore` and `AuditStore`.

Service-owned stores live under their service: the supervise `queue_store`, the
`secret_store`, the orchestrator `config_store`, and the orchestrator
`store_manager` are under `bot_bottle.orchestrator.store`.

Callers import the concrete module they need directly, e.g.
`from bot_bottle.store.db_store import DbStore`.
"""
