"""SQLite migration runner for bot-bottle stores."""

from __future__ import annotations

import sqlite3


class TableMigrations:
    """Runs sequential DDL migrations, tracked by schema_key in
    schema_versions."""

    def __init__(self, schema_key: str, migrations: list[str]) -> None:
        self.schema_key = schema_key
        self.migrations = migrations

    def apply(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                module TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        row = conn.execute(
            "SELECT version FROM schema_versions WHERE module = ?",
            (self.schema_key,),
        ).fetchone()
        version = row[0] if row else 0
        for i, sql in enumerate(self.migrations[version:], start=version + 1):
            conn.execute(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_versions (module, version) VALUES (?, ?)",
                (self.schema_key, i),
            )


__all__ = ["TableMigrations"]
