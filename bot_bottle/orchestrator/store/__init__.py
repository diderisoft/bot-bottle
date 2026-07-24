"""Orchestrator-owned SQLite stores.

The stores whose tables only the orchestrator (control plane) opens: the
supervise proposal/response `queue_store`, the agent-secret `secret_store`, and
the orchestrator `config_store`. They build on the shared `DbStore` /
`migrations` base in `bot_bottle.store`.

Callers import the concrete module directly, e.g.
`from bot_bottle.orchestrator.store.queue_store import QueueStore`.
"""
