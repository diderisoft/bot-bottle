"""The agent bot-bottle remembers for you (`agent_prefs`, in the host DB): one
remembered agent, with per-directory overrides.

- A bare `bot-bottle` starts the agent last used in the directory, else the
  remembered one (`bot-bottle agents default`).
- The guided `bot-bottle start` offers the remembered one and remembers what
  it ran.

The host controller writes it; the CLI asks it (`AGENT_PREFS_PATH`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations


@dataclass(frozen=True)
class AgentPrefs:
    default: str = ""  # the remembered agent, "" for none
    dirs: dict[str, str] = field(default_factory=dict)  # directory -> agent last used there

    def to_json(self) -> dict[str, object]:
        return {"default": self.default, "dirs": dict(self.dirs)}

    @classmethod
    def from_json(cls, raw: object) -> "AgentPrefs":
        body = raw if isinstance(raw, dict) else {}
        dirs = body.get("dirs")
        return cls(default=str(body.get("default") or ""),
                   dirs={str(k): str(v) for k, v in dirs.items()} if isinstance(dirs, dict)
                   else {})


class AgentPrefsStore(DbStore):
    """`agent_prefs`: the remembered agent (`dir` "") and each directory's."""

    TABLES = {"agent_prefs": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("agent_prefs", [
            """
            CREATE TABLE agent_prefs (
                dir TEXT PRIMARY KEY,
                agent TEXT NOT NULL
            )
            """,
        ]))
        self.migrate()

    def prefs(self) -> AgentPrefs:
        with self._connection() as conn:
            rows = {r["dir"]: r["agent"] for r in conn.execute("SELECT * FROM agent_prefs")}
        return AgentPrefs(default=rows.pop("", ""), dirs=rows)

    def remember(self, agent: str, directory: str = "") -> AgentPrefs:
        """Remember `agent` for `directory`, or as the remembered one."""
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO agent_prefs VALUES (?, ?)", (directory, agent))
        self._chmod()
        return self.prefs()


__all__ = ["AgentPrefs", "AgentPrefsStore"]
