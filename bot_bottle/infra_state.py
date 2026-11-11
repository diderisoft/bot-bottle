"""What infrastructure bring-up records about this environment (`infra_state`,
in the host DB, machine-specific), by name:

- `orchestrator.backend`, `orchestrator.url`: the environment's orchestrator,
  its backend and host URL
- `<backend>.settings`, `<backend>.fingerprint`: the `InfraSettings` (JSON)
  the backend's pair was last (re)started with
- `docker.gateway_subnet`: a named environment's docker gateway subnet
- `docker.port.<container>`: the host port of a named environment's docker
  orchestrator

Bring-up runs in the host controller, the only writer. Other processes read
it from a running host controller and never start one for it (`bot-bottle
down`, `doctor`, `version`): with none running, nothing is recorded as far as
they can tell.
"""

from __future__ import annotations

from pathlib import Path

from . import host_access
from .contract.hostctl import INFRA_STATE_PATH
from .lib.db_store import MACHINE_SPECIFIC, DbStore
from .lib.migrations import TableMigrations


class InfraStateStore(DbStore):
    TABLES = {"infra_state": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("infra_state", [
            """
            CREATE TABLE infra_state (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ]))
        self.migrate()

    def all(self) -> dict[str, str]:
        with self._connection() as conn:
            return {r["name"]: r["value"] for r in conn.execute("SELECT * FROM infra_state")}

    def set(self, values: dict[str, str]) -> None:
        """Record `values`, all at once."""
        with self._connection() as conn:
            conn.executemany("INSERT OR REPLACE INTO infra_state VALUES (?, ?)",
                             list(values.items()))
        self._chmod()


def _store() -> InfraStateStore:
    from .paths import host_db_path  # pylint: disable=import-outside-toplevel
    return InfraStateStore(host_db_path())


def recorded() -> dict[str, str]:
    """Everything recorded; {} outside the host controller when none runs."""
    if host_access.in_host_controller():
        return _store().all()
    reply = host_access.ask_running(INFRA_STATE_PATH)
    state = reply.get("state") if reply is not None else None
    return {str(k): str(v) for k, v in state.items()} if isinstance(state, dict) else {}


def get(name: str) -> str:
    """The value recorded as `name`, or ""."""
    return recorded().get(name, "")


def record(values: dict[str, str]) -> None:
    """Record `values`. Only bring-up does, in the host controller."""
    if not host_access.in_host_controller():
        raise RuntimeError("only the host controller records infrastructure state")
    _store().set(values)


__all__ = ["InfraStateStore", "get", "record", "recorded"]
