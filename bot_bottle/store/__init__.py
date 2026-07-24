"""SQLite-backed persistence for bot-bottle.

The store family: a shared `DbStore` base (versioned `migrations`) and the
concrete stores built on it — `ConfigStore`, `AuditStore`, `QueueStore` — plus
`StoreManager`, which migrates them together against the one per-host DB.

Callers import the concrete module they need directly, e.g.
`from bot_bottle.store.store_manager import StoreManager`.
"""
