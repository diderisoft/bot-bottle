"""Per-bottle state: launch metadata for `resume` in the host DB's
`bottled_agents` (ADR 0020), and under `~/.bot-bottle/state/<identity>/` an
optional per-bottle Dockerfile override, per-service scratch dirs and the
preserve marker.

Each `start` mints a fresh identity (`bottle_identity`); `resume <identity>`
relaunches from its recorded metadata.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from . import host_access
from .contract.hostctl import BOTTLED_AGENTS_PATH, BOTTLED_AGENTS_REMOVE_PATH
from .environment import active_name, is_live
from .lib.db_store import MACHINE_SPECIFIC, DbStore
from .lib.migrations import TableMigrations
from .lib.safe_path import check_id, under
from .paths import bot_bottle_root


# Directory layout: ~/.bot-bottle/state/<identity>/...
_STATE_SUBDIR = "state"
_PER_BOTTLE_DOCKERFILE_NAME = "Dockerfile"
_COMMITTED_IMAGE_NAME = "committed-image"
_COMMITTED_ROOTFS_NAME = "committed-rootfs.tar"
# Per-service scratch subdirs.
_EGRESS_SUBDIR = "egress"
_GIT_GATE_SUBDIR = "git-gate"
_SUPERVISE_SUBDIR = "supervise"
_AGENT_SUBDIR = "agent"
# Empty marker: preserve the state dir at session end for `resume`.
_PRESERVE_MARKER = ".preserve"


def bottle_identity(_agent_name: str = "") -> str:
    """Mint a fresh UUID for a new bottle launch. The UUID is the stable
    identity key; the human label is stored separately in metadata."""
    return str(uuid.uuid4())


def globalize_slug(slug: str) -> str:
    """`slug` qualified with this hostname, and the environment's name in a
    named one, for names that must be unique across hosts and the
    environments on them (e.g. deploy-key titles)."""
    env = active_name()
    where = socket.gethostname() if is_live(env) else f"{socket.gethostname()}-{env}"
    return f"{where}-{slug}"


@dataclass(frozen=True)
class BottleMetadata:
    """How a bottle was launched; read by `resume`. A row of the host DB's
    `bottled_agents`, the one place its label is kept."""

    identity: str
    agent_name: str
    cwd: str  # empty when the workspace wasn't copied
    copy_cwd: bool
    started_at: str  # ISO 8601 UTC
    # Derivable from identity; stored so tooling needn't import compose.
    compose_project: str = ""
    backend: str = ""
    label: str = ""
    color: str = ""
    # Empty when the launch picked none: resume uses the default bottle.
    bottle_names: tuple[str, ...] = ()
    # Who started it ("console" or "terminal") and, for the console, its id
    # for the agent, so a resume from anywhere comes back as the same agent.
    # Recorded by the host controller once the agent runs.
    origin: str = ""
    agent_id: str = ""
    # The parent bottle's identity when this bottle was launched by another
    # bottle via `launch-bottle` (PRD 0106); empty otherwise.
    parent: str = ""

    def to_json(self) -> dict[str, object]:
        return {**dataclasses.asdict(self), "bottle_names": list(self.bottle_names)}

    @classmethod
    def from_json(cls, raw: object) -> "BottleMetadata":
        """ValueError unless `raw` is `to_json()`'s shape."""
        if not isinstance(raw, dict):
            raise ValueError("a bottled agent is an object")
        body = cast(dict[str, object], raw)
        names = body.get("bottle_names", [])
        if not isinstance(names, list):
            raise ValueError("bottle_names is a list")
        fields = {f.name for f in dataclasses.fields(cls)} - {"bottle_names", "copy_cwd"}
        values = {name: body.get(name, "") for name in fields}
        if not all(isinstance(v, str) for v in values.values()):
            raise ValueError("a bottled agent's fields are text")
        # Its state dir is named after it.
        check_id(values["identity"], "a bottled agent's identity")
        return cls(**cast(dict[str, str], values), copy_cwd=body.get("copy_cwd") is True,
                   bottle_names=tuple(str(n) for n in names))


# What may change on a recorded bottled agent (`update_metadata`).
CHANGEABLE = frozenset({"label", "origin", "agent_id", "parent"})
# The per-bottled-agent tables its removal clears with it, beside its own.
_ROWS_OF_A_BOTTLED_AGENT = (("sessions", "id"), ("snapshots", "session"))


class BottledAgentStore(DbStore):
    """`bottled_agents`: each bottled agent's launch metadata and, once it
    runs, how a restarted host controller finishes it (`adoption`). The host
    controller writes it; the CLI asks it (`BOTTLED_AGENTS_PATH`)."""

    TABLES = {"bottled_agents": MACHINE_SPECIFIC}
    _COLUMNS = ("identity", "agent_name", "cwd", "copy_cwd", "started_at", "compose_project",
                "backend", "label", "color", "bottle_names", "origin", "agent_id", "parent")

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("bottled_agents", [
            """
            CREATE TABLE bottled_agents (
                identity TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                cwd TEXT NOT NULL,
                copy_cwd INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                compose_project TEXT NOT NULL,
                backend TEXT NOT NULL,
                label TEXT NOT NULL,
                color TEXT NOT NULL,
                bottle_names TEXT NOT NULL,
                origin TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                parent TEXT NOT NULL,
                adoption TEXT
            )
            """,
        ]))
        self.migrate()

    @staticmethod
    def _row(row: sqlite3.Row) -> BottleMetadata:
        return BottleMetadata(**{k: row[k] for k in BottledAgentStore._COLUMNS
                                 if k not in ("copy_cwd", "bottle_names")},
                              copy_cwd=bool(row["copy_cwd"]),
                              bottle_names=tuple(json.loads(row["bottle_names"])))

    def get(self, identity: str) -> BottleMetadata | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM bottled_agents WHERE identity = ?",
                               (identity,)).fetchone()
        return self._row(row) if row is not None else None

    def all(self) -> list[BottleMetadata]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM bottled_agents ORDER BY started_at").fetchall()
        return [self._row(r) for r in rows]

    def put(self, metadata: BottleMetadata) -> None:
        """Record a launch; a resume's replaces the last one's metadata."""
        check_id(metadata.identity, "a bottled agent's identity")
        values = {**dataclasses.asdict(metadata), "copy_cwd": int(metadata.copy_cwd),
                  "bottle_names": json.dumps(list(metadata.bottle_names))}
        columns = ", ".join(self._COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in self._COLUMNS[1:])
        with self._connection() as conn:
            conn.execute(
                f"INSERT INTO bottled_agents ({columns}) VALUES "
                f"({', '.join('?' * len(self._COLUMNS))}) "
                f"ON CONFLICT(identity) DO UPDATE SET {updates}",
                tuple(values[c] for c in self._COLUMNS))
        self._chmod()

    def update(self, identity: str, changes: dict[str, str]) -> BottleMetadata | None:
        """Change `changes`' fields (`CHANGEABLE`) of `identity`; it as it
        now is, or None when there's no such bottled agent."""
        unknown = set(changes) - CHANGEABLE
        if unknown:
            raise ValueError(f"can't change {', '.join(sorted(unknown))}")
        with self._write_connection() as conn:
            if changes:
                conn.execute(
                    f"UPDATE bottled_agents SET {', '.join(f'{k} = ?' for k in changes)} "
                    "WHERE identity = ?", (*changes.values(), identity))
            row = conn.execute("SELECT * FROM bottled_agents WHERE identity = ?",
                               (identity,)).fetchone()
        self._chmod()
        return self._row(row) if row is not None else None

    def adoption(self, identity: str) -> str:
        with self._connection() as conn:
            row = conn.execute("SELECT adoption FROM bottled_agents WHERE identity = ?",
                               (identity,)).fetchone()
        return str(row["adoption"] or "") if row is not None else ""

    def set_adoption(self, identity: str, adoption: str) -> bool:
        """Record how to finish `identity`; False when it isn't recorded."""
        with self._connection() as conn:
            changed = conn.execute("UPDATE bottled_agents SET adoption = ? WHERE identity = ?",
                                   (adoption, identity)).rowcount
        self._chmod()
        return changed > 0

    def remove(self, identity: str) -> bool:
        """Delete `identity`'s rows here and in its other tables, at once.
        Whether it was recorded."""
        with self._write_connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table, key in _ROWS_OF_A_BOTTLED_AGENT:
                if table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE {key} = ?", (identity,))
            removed = conn.execute("DELETE FROM bottled_agents WHERE identity = ?",
                                   (identity,)).rowcount
        self._chmod()
        return removed > 0


def store() -> BottledAgentStore:
    """The host DB's bottled agents. Only in the host controller."""
    from .paths import host_db_path  # pylint: disable=import-outside-toplevel
    return BottledAgentStore(host_db_path())


def _one(reply: dict[str, object]) -> BottleMetadata | None:
    found = reply.get("bottled_agent")
    return BottleMetadata.from_json(found) if found is not None else None


def write_metadata(metadata: BottleMetadata) -> None:
    """Record a launch of `metadata.identity`."""
    if host_access.in_host_controller():
        store().put(metadata)
    else:
        host_access.ask(BOTTLED_AGENTS_PATH, {"metadata": metadata.to_json()})


def read_metadata(identity: str) -> BottleMetadata | None:
    """The recorded metadata for `identity`, or None."""
    if host_access.in_host_controller():
        return store().get(identity)
    return next((m for m in all_metadata() if m.identity == identity), None)


def all_metadata() -> list[BottleMetadata]:
    """Every bottled agent this host records, oldest launch first."""
    if host_access.in_host_controller():
        return store().all()
    found = host_access.ask(BOTTLED_AGENTS_PATH).get("bottled_agents")
    return [BottleMetadata.from_json(m) for m in (found if isinstance(found, list) else [])]


def update_metadata(identity: str, **changes: str) -> BottleMetadata | None:
    """Change `changes` (`CHANGEABLE`) on `identity`'s record; it as it now
    is, or None when nothing records it."""
    if host_access.in_host_controller():
        return store().update(identity, changes)
    return _one(host_access.ask(BOTTLED_AGENTS_PATH,
                                {"identity": identity, "changes": dict(changes)}))


def record_origin(identity: str, origin: str, agent_id: str = "") -> None:
    """Note who started `identity` (`BottleMetadata.origin`, `agent_id`)."""
    update_metadata(identity, origin=origin, agent_id=agent_id)


# A bottled agent's label: what people call it. Its identity never changes
# with it (state dirs, VMs, transcripts and deploy keys are keyed by that).
MAX_LABEL_CHARS = 128


def clean_label(label: object) -> str:
    """`label` trimmed, or ValueError saying what's wrong with it."""
    if not isinstance(label, str):
        raise ValueError("a label is text")
    label = label.strip()
    if not label:
        raise ValueError("a label can't be empty")
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError(f"a label is at most {MAX_LABEL_CHARS} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise ValueError("a label can't hold control characters")
    return label


def rename_label(identity: str, label: str) -> bool:
    """Give `identity` the label `label` (already clean). False when nothing
    records it."""
    return update_metadata(identity, label=label) is not None


def record_parent(identity: str, parent: str) -> None:
    """Note that `identity` was launched by `parent` via `launch-bottle`."""
    update_metadata(identity, parent=parent)


def console_agent_id(identity: str) -> str:
    """The console's id for the agent `identity` last ran as, when the console
    started it; empty otherwise."""
    current = read_metadata(identity)
    if current is None or current.origin != "console":
        return ""
    return current.agent_id


def state_root() -> Path:
    """Where every bottled agent's state dir is."""
    return bot_bottle_root() / _STATE_SUBDIR


def bottle_state_dir(identity: str) -> Path:
    """The bottle's state dir. Created lazily by writers; readers tolerate
    absence."""
    return state_root() / identity


def per_bottle_dockerfile_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _PER_BOTTLE_DOCKERFILE_NAME


def per_bottle_dockerfile(identity: str) -> str | None:
    """The per-bottle Dockerfile override, or None."""
    p = per_bottle_dockerfile_path(identity)
    if p.is_file():
        return p.read_text()
    return None


def write_per_bottle_dockerfile(identity: str, content: str) -> Path:
    p = per_bottle_dockerfile_path(identity)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    p.chmod(0o644)
    return p


def committed_image_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _COMMITTED_IMAGE_NAME


def committed_rootfs_path(identity: str) -> Path:
    """Where the Firecracker freezer stores the bottle's rootfs snapshot (a
    tar), which `resume` boots from."""
    return bottle_state_dir(identity) / _COMMITTED_ROOTFS_NAME


def write_committed_image(identity: str, image_tag: str) -> Path:
    """Record the committed image for `identity`; `resume` boots from it."""
    path = committed_image_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(image_tag.strip() + "\n")
    path.chmod(0o644)
    return path


def read_committed_image(identity: str) -> str | None:
    """The committed image tag for `identity`, or None."""
    path = committed_image_path(identity)
    if not path.is_file():
        return None
    tag = path.read_text().strip()
    return tag or None


# Per-service scratch subdirs, removed with the state dir by `remove_bottled_agent`.


def egress_state_dir(identity: str) -> Path:
    """Egress config, including the staged gateway CA (`gateway-ca/`)."""
    return bottle_state_dir(identity) / _EGRESS_SUBDIR


def git_gate_state_dir(identity: str) -> Path:
    """git-gate entrypoint, hooks and per-upstream known_hosts."""
    return bottle_state_dir(identity) / _GIT_GATE_SUBDIR


def supervise_state_dir(identity: str) -> Path:
    """Reserved for supervise mount sources; queue rows live in the host DB."""
    return bottle_state_dir(identity) / _SUPERVISE_SUBDIR


def agent_state_dir(identity: str) -> Path:
    """The agent's env file and prompt file."""
    return bottle_state_dir(identity) / _AGENT_SUBDIR




def preserve_marker_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _PRESERVE_MARKER


def mark_preserved(identity: str) -> Path:
    """Keep this bottle's state dir at session end for `resume`."""
    path = preserve_marker_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def is_preserved(identity: str) -> bool:
    return preserve_marker_path(identity).exists()


def clear_preserve_marker(identity: str) -> None:
    """Remove a stale marker at launch. Idempotent."""
    try:
        preserve_marker_path(identity).unlink()
    except FileNotFoundError:
        pass


def remove_bottled_agent(identity: str) -> bool:
    """Delete the bottled agent: its state dir and its host DB rows, in one
    call to the host controller (`delete_bottled_agent`). Whether anything
    recorded it. Idempotent."""
    if host_access.in_host_controller():
        return delete_bottled_agent(identity)
    return host_access.ask(BOTTLED_AGENTS_REMOVE_PATH, {"identity": identity}).get(
        "removed") is True


def delete_bottled_agent(identity: str) -> bool:
    """`remove_bottled_agent`, in the host controller: the state dir, its
    snapshots' patches, and its rows (`BottledAgentStore.remove`)."""
    import shutil  # pylint: disable=import-outside-toplevel
    from .control.hostctl.snapshots import snapshots_root  # pylint: disable=import-outside-toplevel
    check_id(identity, "a bottled agent's identity")
    shutil.rmtree(under(state_root(), identity), ignore_errors=True)
    shutil.rmtree(under(snapshots_root(), identity), ignore_errors=True)
    return store().remove(identity)


def capture_session_state(identity: str, exit_code: int) -> None:
    """After an agent's session ends: keep its state (and its home) for
    `resume` if the agent exited non-zero, as a stop does."""
    if identity and exit_code != 0:
        mark_preserved(identity)


def settle_state(identity: str) -> None:
    """After teardown: print the resume hint if preserved, else remove the
    state dir."""
    from .lib.log import info  # pylint: disable=import-outside-toplevel
    if not identity:
        return
    if is_preserved(identity):
        info(f"to resume this bottled agent: bot-bottle resume {identity}")
        return
    remove_bottled_agent(identity)


__all__ = [
    "BottleMetadata",
    "BottledAgentStore",
    "CHANGEABLE",
    "MAX_LABEL_CHARS",
    "all_metadata",
    "record_origin",
    "delete_bottled_agent",
    "remove_bottled_agent",
    "store",
    "update_metadata",
    "clean_label",
    "rename_label",
    "record_parent",
    "capture_session_state",
    "settle_state",
    "agent_state_dir",
    "bottle_identity",
    "globalize_slug",
    "bottle_state_dir",
    "state_root",
    "clear_preserve_marker",
    "committed_image_path",
    "committed_rootfs_path",
    "egress_state_dir",
    "git_gate_state_dir",
    "is_preserved",
    "mark_preserved",
    "per_bottle_dockerfile",
    "per_bottle_dockerfile_path",
    "preserve_marker_path",
    "read_committed_image",
    "read_metadata",
    "supervise_state_dir",
    "write_committed_image",
    "write_metadata",
    "write_per_bottle_dockerfile",
]
