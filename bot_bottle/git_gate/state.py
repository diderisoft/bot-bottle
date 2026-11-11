"""What the host keeps of a bottled agent's git-gate, to provision its gateway
again (a cold gateway, a repo granted while it runs), renew its GitHub tokens
and revoke its deploy keys: rows of the host DB, keyed by the bottled agent's
identity (ADR 0020).

- `git_gate_upstreams`: its resolved upstream table, as the gateway was last
  given it, one row per repo.
- `git_gate_live_grants`: the deploy keys granted while it runs (PRD 0105),
  one row per repo, revoked at teardown with the launch's own.
- `git_gate_trailers`: the `Bot-Bottle` trailer its commits carry and how
  the check treats one without it.

Only the host controller, where launches, grants, re-provisioning and token
renewal run, reads and writes them. The deploy keys themselves stay files in
the bottled agent's git-gate state dir. Removing a bottled agent deletes its
rows (`bottle_state.BottledAgentStore.remove`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import host_access
from ..control.git_gate_render import GitGateUpstream
from ..lib.db_store import MACHINE_SPECIFIC, DbStore
from ..lib.migrations import TableMigrations

# A live grant's columns beside its identity, name and position: what
# revoking its key needs (`provision.DeployKeyRef`).
GRANT_FIELDS = ("forge_token_env", "api_url", "upstream_host", "upstream_path", "grant",
                "provider")
_UPSTREAM_FIELDS = ("upstream_url", "upstream_host", "upstream_port", "identity_file",
                    "known_host_key", "known_hosts_file", "writable", "transport",
                    "token_secret", "protect_default_branch")


class GitGateStore(DbStore):
    """`git_gate_upstreams`, `git_gate_live_grants` and `git_gate_trailers`.
    Rows of a bottled agent's repos keep their order in `position`."""

    TABLES = {"git_gate_upstreams": MACHINE_SPECIFIC,
              "git_gate_live_grants": MACHINE_SPECIFIC,
              "git_gate_trailers": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("git_gate", [
            """
            CREATE TABLE git_gate_upstreams (
                identity TEXT NOT NULL,
                name TEXT NOT NULL,
                position INTEGER NOT NULL,
                upstream_url TEXT NOT NULL,
                upstream_host TEXT NOT NULL,
                upstream_port TEXT NOT NULL,
                identity_file TEXT NOT NULL,
                known_host_key TEXT NOT NULL,
                known_hosts_file TEXT NOT NULL,
                writable INTEGER NOT NULL,
                transport TEXT NOT NULL,
                token_secret TEXT NOT NULL,
                protect_default_branch INTEGER NOT NULL,
                PRIMARY KEY (identity, name)
            )
            """,
            """
            CREATE TABLE git_gate_live_grants (
                identity TEXT NOT NULL,
                name TEXT NOT NULL,
                position INTEGER NOT NULL,
                forge_token_env TEXT NOT NULL,
                api_url TEXT NOT NULL,
                upstream_host TEXT NOT NULL,
                upstream_path TEXT NOT NULL,
                "grant" TEXT NOT NULL,
                provider TEXT NOT NULL,
                PRIMARY KEY (identity, name)
            )
            """,
            """
            CREATE TABLE git_gate_trailers (
                identity TEXT PRIMARY KEY,
                trailer TEXT NOT NULL,
                mode TEXT NOT NULL
            )
            """,
        ]))
        self.migrate()

    # --- upstreams ---

    def upstreams(self, identity: str) -> tuple[GitGateUpstream, ...]:
        """Its upstreams in order, each with its known_hosts file when that
        is still there."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM git_gate_upstreams WHERE identity = ? "
                                "ORDER BY position", (identity,)).fetchall()
        return tuple(self._upstream(r) for r in rows)

    @staticmethod
    def _upstream(r: sqlite3.Row) -> GitGateUpstream:
        known_hosts = Path(r["known_hosts_file"]) if r["known_hosts_file"] else Path()
        return GitGateUpstream(
            name=r["name"], upstream_url=r["upstream_url"],
            upstream_host=r["upstream_host"], upstream_port=r["upstream_port"],
            identity_file=r["identity_file"], known_host_key=r["known_host_key"],
            known_hosts_file=known_hosts if known_hosts.is_file() else Path(),
            writable=bool(r["writable"]), transport=r["transport"],
            token_secret=r["token_secret"],
            protect_default_branch=bool(r["protect_default_branch"]))

    @staticmethod
    def _upstream_row(identity: str, position: int,
                      u: GitGateUpstream) -> tuple[object, ...]:
        known_hosts = str(u.known_hosts_file) if u.known_hosts_file != Path() else ""
        return (identity, u.name, position, u.upstream_url, u.upstream_host, u.upstream_port,
                u.identity_file, u.known_host_key, known_hosts, int(u.writable), u.transport,
                u.token_secret, int(u.protect_default_branch))

    _INSERT_UPSTREAM = (f"INSERT INTO git_gate_upstreams (identity, name, position, "
                        f"{', '.join(_UPSTREAM_FIELDS)}) VALUES ({', '.join('?' * 13)})")

    def set_upstreams(self, identity: str, upstreams: tuple[GitGateUpstream, ...]) -> None:
        """Make `upstreams` its whole table."""
        with self._write_connection() as conn:
            conn.execute("DELETE FROM git_gate_upstreams WHERE identity = ?", (identity,))
            conn.executemany(self._INSERT_UPSTREAM,
                             [self._upstream_row(identity, i, u)
                              for i, u in enumerate(upstreams)])
        self._chmod()

    def put_upstream(self, identity: str, upstream: GitGateUpstream) -> None:
        """Add `upstream`, last, replacing the one of the same name."""
        with self._write_connection() as conn:
            conn.execute("DELETE FROM git_gate_upstreams WHERE identity = ? AND name = ?",
                         (identity, upstream.name))
            conn.execute(self._INSERT_UPSTREAM,
                         self._upstream_row(identity, _next(conn, "git_gate_upstreams",
                                                            identity), upstream))
        self._chmod()

    def drop_upstream(self, identity: str, name: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM git_gate_upstreams WHERE identity = ? AND name = ?",
                         (identity, name))

    # --- live grants ---

    def live_grants(self, identity: str) -> list[dict[str, str]]:
        """Each live grant's `name` and `GRANT_FIELDS`, oldest first."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM git_gate_live_grants WHERE identity = ? "
                                "ORDER BY position", (identity,)).fetchall()
        return [{k: str(r[k]) for k in ("name", *GRANT_FIELDS)} for r in rows]

    def record_live_grant(self, identity: str, grant: dict[str, str]) -> None:
        """Record `grant` last, replacing the one for the same repo name (a
        re-grant), in one transaction."""
        with self._write_connection() as conn:
            conn.execute("DELETE FROM git_gate_live_grants WHERE identity = ? AND name = ?",
                         (identity, grant["name"]))
            conn.execute(
                'INSERT INTO git_gate_live_grants (identity, name, position, forge_token_env, '
                'api_url, upstream_host, upstream_path, "grant", provider) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identity, grant["name"], _next(conn, "git_gate_live_grants", identity),
                 *(grant[k] for k in GRANT_FIELDS)))
        self._chmod()

    def forget_live_grant(self, identity: str, name: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM git_gate_live_grants WHERE identity = ? AND name = ?",
                         (identity, name))

    # --- trailer ---

    def trailer(self, identity: str) -> tuple[str, str]:
        """(trailer, mode), or ("", "") when none is recorded."""
        with self._connection() as conn:
            row = conn.execute("SELECT trailer, mode FROM git_gate_trailers WHERE identity = ?",
                               (identity,)).fetchone()
        return ("", "") if row is None else (str(row["trailer"]), str(row["mode"]))

    def set_trailer(self, identity: str, trailer: str, mode: str) -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO git_gate_trailers VALUES (?, ?, ?)",
                         (identity, trailer, mode))
        self._chmod()


def _next(conn: sqlite3.Connection, table: str, identity: str) -> int:
    """The position after `identity`'s last row of `table`."""
    row = conn.execute(
        f"SELECT MAX(position) FROM {table} WHERE identity = ?", (identity,)).fetchone()
    return 0 if row[0] is None else int(row[0]) + 1


def store() -> GitGateStore:
    """The host DB's git-gate rows. Only in the host controller."""
    if not host_access.in_host_controller():
        raise RuntimeError("only the host controller keeps git-gate state")
    from ..paths import host_db_path  # pylint: disable=import-outside-toplevel
    return GitGateStore(host_db_path())


__all__ = ["GRANT_FIELDS", "GitGateStore", "store"]
