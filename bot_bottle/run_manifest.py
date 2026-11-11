"""A bottled agent's manifest versions (PRD 0107): its resolved bottle as one
document (`manifest.document`), as it launched and as saved since.

Each version's document is a file in the bottle's state dir,
`manifest/vNNNN.yaml`. When and why each was written is a row of the host DB's
`run_manifest_versions`. Version 1 is the bottle the agent launched with; the
console's Save adds one with the run's changes. `run_manifest_launches` holds
what its bottles were at the latest launch or resume (PRD 0111): each one's
fingerprint (`manifest.fingerprint`) and the host's bottle version then.

Only the host controller, where launches and the console's Save run, reads
and writes them (ADR 0020).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bottle_state import bottle_state_dir
from .contract.yaml_subset import parse_frontmatter, serialize_yaml_subset
from .lib.db_store import MACHINE_SPECIFIC, DbStore
from .lib.migrations import TableMigrations


class RunManifestStore(DbStore):
    """`run_manifest_versions` (one row per version) and
    `run_manifest_launches` (one per bottled agent), keyed by identity."""

    TABLES = {"run_manifest_versions": MACHINE_SPECIFIC,
              "run_manifest_launches": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("run_manifest", [
            """
            CREATE TABLE run_manifest_versions (
                identity TEXT NOT NULL,
                version INTEGER NOT NULL,
                message TEXT NOT NULL,
                at TEXT NOT NULL,
                PRIMARY KEY (identity, version)
            )
            """,
            # `bottles`: name -> fingerprint, in chain order.
            """
            CREATE TABLE run_manifest_launches (
                identity TEXT PRIMARY KEY,
                bottles TEXT NOT NULL,
                host TEXT NOT NULL
            )
            """,
        ]))
        self.migrate()

    def versions(self, identity: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT version, message, at FROM run_manifest_versions "
                                "WHERE identity = ? ORDER BY version", (identity,)).fetchall()
        return [{"version": r["version"], "message": r["message"], "at": r["at"]} for r in rows]

    def add_version(self, identity: str, message: str, at: str,
                    write: Callable[[int], object]) -> int:
        """Add the next version: `write(number)` writes its document, then its
        row goes in, in one transaction. Its number."""
        with self._write_connection() as conn:
            row = conn.execute("SELECT MAX(version) FROM run_manifest_versions "
                               "WHERE identity = ?", (identity,)).fetchone()
            number = int(row[0] or 0) + 1
            write(number)
            conn.execute("INSERT INTO run_manifest_versions VALUES (?, ?, ?, ?)",
                         (identity, number, message, at))
        self._chmod()
        return number

    def launch(self, identity: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT bottles, host FROM run_manifest_launches "
                               "WHERE identity = ?", (identity,)).fetchone()
        return {} if row is None else {"bottles": json.loads(row["bottles"]),
                                       "host": row["host"]}

    def set_launch(self, identity: str, bottles: dict[str, str], host: str) -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO run_manifest_launches VALUES (?, ?, ?)",
                         (identity, json.dumps(bottles), host))
        self._chmod()


def _store() -> RunManifestStore:
    # pylint: disable-next=import-outside-toplevel
    from . import host_access
    if not host_access.in_host_controller():
        raise RuntimeError("only the host controller records manifest versions")
    from .paths import host_db_path  # pylint: disable=import-outside-toplevel
    return RunManifestStore(host_db_path())


def _dir(identity: str) -> Path:
    return bottle_state_dir(identity) / "manifest"


def versions(identity: str) -> list[dict[str, Any]]:
    """Each version's `version`, `at` (ISO) and `message`, oldest first."""
    return _store().versions(identity)


def document_text(doc: dict[str, object]) -> str:
    """A manifest document as YAML."""
    return serialize_yaml_subset(doc)


def read(identity: str, version: int) -> dict[str, object]:
    """Version `version`'s document."""
    text = (_dir(identity) / f"v{version:04d}.yaml").read_text(encoding="utf-8")
    return dict(parse_frontmatter(f"---\n{text}---\n")[0])


def latest(identity: str) -> tuple[dict[str, Any], dict[str, object]] | None:
    """The newest version's log entry and document, or None."""
    log = versions(identity)
    if not log:
        return None
    return log[-1], read(identity, int(log[-1]["version"]))


def record(identity: str, doc: dict[str, object], message: str) -> int:
    """Write `doc` as the next version; returns its number."""
    directory = _dir(identity)
    directory.mkdir(parents=True, exist_ok=True)
    text = document_text(doc)
    return _store().add_version(
        identity, message, datetime.now(timezone.utc).isoformat(timespec="seconds"),
        lambda n: (directory / f"v{n:04d}.yaml").write_text(text, encoding="utf-8"))


def launch_record(identity: str) -> dict[str, Any]:
    """`bottles` (name -> fingerprint, in chain order) and the host's bottle
    version (`host`, a commit) at the latest launch, or {} if there's none."""
    return _store().launch(identity)


def record_launch(identity: str, bottle: Any, *, names: tuple[str, ...] = (), cwd: str = "",
                  host_version: str = "") -> None:
    """Version 1 at launch. A resume keeps the history, adding a version only
    if the bottle files changed since. The fingerprints of `names` (the
    bottles it launched in, resolved from `cwd`) and `host_version` become
    its `launch_record`, at every launch and resume. Never raises: the launch
    goes on."""
    # pylint: disable=import-outside-toplevel
    from .manifest.document import bottle_document
    from .manifest.fingerprint import chain_hashes
    from .manifest.index import ManifestIndex
    try:
        doc = bottle_document(bottle)
        last = latest(identity)
        if last is None:
            record(identity, doc, "launched")
        elif document_text(last[1]) != document_text(doc):
            record(identity, doc, "resumed with the bottle files as they are now")
        chain = chain_hashes(ManifestIndex.resolve(cwd or str(Path.home())), names)
        _store().set_launch(identity, dict(chain), host_version)
    except Exception as e:  # pylint: disable=broad-exception-caught  # the launch goes on
        # pylint: disable-next=import-outside-toplevel
        from .lib.log import warn
        warn(f"bottle {identity}: couldn't record its manifest version: {e}")


__all__ = ["RunManifestStore", "document_text", "latest", "launch_record", "read", "record",
           "record_launch", "versions"]
