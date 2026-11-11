"""SQLite stores only the orchestrator opens, all in its `registry.db`:
`registry_store`, `queue_store` and `canary_store` (`secret_store` encrypts
the registry's egress secrets), on the
`lib` `DbStore` base. Import the modules directly.
"""
