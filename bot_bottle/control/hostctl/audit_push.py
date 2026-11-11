"""Audit records: each console agent's conversation, encrypted on the host and
sent to the console, which keeps only ciphertext (ADR 0013).

One `AuditPusher` per agent. Each entry of its conversation (as the host's
transcript copy holds it, masked: `transcript_serve`) is encrypted with the
host's AES-256-GCM audit key (`<root>/audit.key`, never sent), bound to its
run and position, and hash-chained to the last record sent for the agent, so
the chain carries on across its runs. How far it got is kept in the host DB
(`audit_push_cursors`, and `audit_push_runs` for each run's next record): a
restarted host controller, a resume or a re-adopted bottle carries on there,
never sending an entry twice. A tick sends at most `BATCH` records, off the
event loop but for the sends themselves.

An agent with no cursor here (a new host database) sends nothing until the
console says where its chain is: the pusher asks `audit_head` (`agent_id`) and
the console answers `audit_head` with the agent's `runs` on this host (`run_id`,
`records`) and its `last` record (`run_id`, `seq`, `ciphertext`, `chain_hash`)
or null. Decrypting that record gives the transcript position it carried. The
cursor is never reset here, which would send every entry again or none.

Reading them back is `decrypt`: only with this host's key, for the record's
own run and seq, and only once the user unlocked with a passkey
(`audit_unlock`).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib import log
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...paths import audit_key_bytes, host_db_path

if TYPE_CHECKING:
    from ...transcript import TranscriptEntry

BATCH = 50
# How often an agent with no cursor asks the console for its chain's head.
ASK_HEAD_SECONDS = 60.0
_NS = uuid.UUID("6f0b6e8c-6c1f-4a8e-9b1a-2f6a6b0c7e51")
_RECORD_TYPES = {"user": "user_message", "assistant": "assistant_message",
                 "tool_call": "tool_call", "tool_result": "tool_result_meta",
                 "system": "system_event"}


def record_type(entry: "TranscriptEntry") -> str:
    return "agent_error" if entry.error else _RECORD_TYPES.get(entry.kind, "system_event")


def record_id(run_id: str, seq: int) -> str:
    """A record's id: the same for the same run and position, so one sent
    twice is the same row."""
    return str(uuid.uuid5(_NS, f"{run_id}:{seq}"))


def _aad(run_id: str, seq: int) -> bytes:
    # Binds a ciphertext to its record: one moved to another run or position
    # doesn't decrypt.
    return f"bot-bottle-audit:{run_id}:{seq}".encode()


def encrypt(key: bytes, run_id: str, seq: int, payload: dict[str, Any]) -> str:
    # pylint: disable-next=import-outside-toplevel
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    plain = json.dumps(payload, separators=(",", ":")).encode()
    return (nonce + AESGCM(key).encrypt(nonce, plain, _aad(run_id, seq))).hex()


def decrypt(key: bytes, run_id: str, seq: int, ciphertext: str) -> str:
    # pylint: disable-next=import-outside-toplevel
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = bytes.fromhex(ciphertext)
    return AESGCM(key).decrypt(raw[:12], raw[12:], _aad(run_id, seq)).decode("utf-8")


@dataclass
class PushState:
    """How far an agent's records have been sent."""
    last_entry_seq: int | None = None  # the transcript's (stable) seq last sent
    chain_hash: str = ""               # the last record's
    runs: dict[str, int] = field(default_factory=dict)  # run id -> its next record seq


class AuditPushStore(DbStore):
    """How far each agent's records were sent: its cursor (the transcript
    entry last sent and the chain's head) and each run's next record."""

    TABLES = {"audit_push_cursors": MACHINE_SPECIFIC, "audit_push_runs": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("audit_push", [
            """
            CREATE TABLE audit_push_cursors (
                agent_id TEXT PRIMARY KEY,
                last_entry_seq INTEGER,
                chain_hash TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE audit_push_runs (
                run_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                next_seq INTEGER NOT NULL
            )
            """,
        ]))
        self.migrate()

    def load(self, agent_id: str) -> PushState | None:
        """`agent_id`'s cursor, or None when this host has none."""
        with self._connection() as conn:
            row = conn.execute("SELECT last_entry_seq, chain_hash FROM audit_push_cursors "
                               "WHERE agent_id = ?", (agent_id,)).fetchone()
            runs = conn.execute("SELECT run_id, next_seq FROM audit_push_runs "
                                "WHERE agent_id = ?", (agent_id,)).fetchall()
        if row is None:
            return None
        return PushState(last_entry_seq=row["last_entry_seq"], chain_hash=row["chain_hash"],
                         runs={r["run_id"]: r["next_seq"] for r in runs})

    def save(self, agent_id: str, state: PushState) -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO audit_push_cursors VALUES (?, ?, ?)",
                         (agent_id, state.last_entry_seq, state.chain_hash))
            conn.executemany("INSERT OR REPLACE INTO audit_push_runs VALUES (?, ?, ?)",
                             [(run, agent_id, seq) for run, seq in state.runs.items()])
        self._chmod()


def rebuilt(key: bytes, reply: dict[str, Any]) -> PushState:
    """The cursor the console's `audit_head` reply says: its runs' record
    counts, and from its last record (decrypted) the entry it carried and the
    chain's head. ValueError when the reply doesn't hold one."""
    runs = reply.get("runs")
    if not isinstance(runs, list):
        raise ValueError("no runs")
    counts = {str(r["run_id"]): int(r["records"]) for r in runs
              if isinstance(r, dict) and r.get("run_id") and isinstance(r.get("records"), int)}
    last = reply.get("last")
    if last is None:
        return PushState(runs=counts)
    if not isinstance(last, dict):
        raise ValueError("last isn't a record")
    try:
        entry = json.loads(decrypt(key, str(last["run_id"]), int(last["seq"]),
                                   str(last["ciphertext"])))
        return PushState(last_entry_seq=int(entry["seq"]), chain_hash=str(last["chain_hash"]),
                         runs=counts)
    except Exception as e:  # pylint: disable=broad-exception-caught  # tampered, or not ours
        raise ValueError(f"its last record doesn't decrypt with this host's key ({e})") from e


class AuditPusher:
    def __init__(self, agent_id: str, key: bytes, entries: Callable[[str], list[Any] | None],
                 send: Callable[[dict[str, Any]], Awaitable[None]], *,
                 store: AuditPushStore | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.agent_id = agent_id
        self._key = key
        self._entries = entries
        self._send = send
        self._store = store or AuditPushStore(host_db_path())
        self._clock = clock
        self._asked: float | None = None
        self._lock = threading.Lock()
        # None until this host has a cursor for the agent (`head`).
        self.state: PushState | None = self._store.load(agent_id)

    def head(self, reply: dict[str, Any]) -> bool:
        """Take the console's `audit_head` answer as the cursor, unless one
        is here already. Whether it was taken."""
        if self.state is not None:
            return False
        try:
            state = rebuilt(self._key, reply)
        except (ValueError, KeyError, TypeError) as e:
            log.warn(f"audit: the console's head for {self.agent_id} won't do: {e}; "
                     "asking again later")
            return False
        self._store.save(self.agent_id, state)
        self.state = state
        return True

    def _batch(self, run_id: str) -> list[dict[str, Any]]:
        """The next records to send, built (and the state advanced) off the loop."""
        assert self.state is not None
        entries = self._entries(self.agent_id) or []
        last = self.state.last_entry_seq
        fresh = [e for e in entries if not e.meta and (last is None or e.seq > last)][:BATCH]
        out: list[dict[str, Any]] = []
        with self._lock:
            seq = self.state.runs.get(run_id, 0)
            chain = self.state.chain_hash
            for entry in fresh:
                ciphertext = encrypt(self._key, run_id, seq, entry.to_json())
                new_chain = hashlib.sha256((chain + ciphertext).encode()).hexdigest()
                out.append({"type": "audit_record", "agent_id": self.agent_id,
                            "run_id": run_id, "id": record_id(run_id, seq), "seq": seq,
                            "ts": entry.ts or datetime.now(timezone.utc).isoformat(),
                            "record_type": record_type(entry), "ciphertext": ciphertext,
                            "chain_hash": new_chain, "prev_chain_hash": chain or None,
                            "_entry_seq": entry.seq})
                chain, seq = new_chain, seq + 1
        return out

    async def push(self, run_id: str) -> int:
        """Send the next batch for `run_id`; how many went."""
        if not run_id:
            return 0
        if self.state is None:
            await self._ask_head()
            return 0
        try:
            batch = await asyncio.to_thread(self._batch, run_id)
        except Exception as e:  # pylint: disable=broad-exception-caught  # tried again next tick
            log.warn(f"audit: couldn't build {self.agent_id}'s records: {e}")
            return 0
        sent = 0
        for record in batch:
            entry_seq = record.pop("_entry_seq")
            try:
                await self._send(record)
            except Exception as e:  # pylint: disable=broad-exception-caught  # the link dropped
                log.warn(f"audit: couldn't send {self.agent_id}'s record: {e}")
                break
            with self._lock:
                assert self.state is not None
                self.state.last_entry_seq = entry_seq
                self.state.chain_hash = record["chain_hash"]
                self.state.runs[run_id] = record["seq"] + 1
            sent += 1
        if sent:
            assert self.state is not None
            try:
                await asyncio.to_thread(self._store.save, self.agent_id, self.state)
            except sqlite3.Error as e:
                log.warn(f"audit: couldn't keep how far {self.agent_id}'s records went: {e}")
        return sent

    async def _ask_head(self) -> None:
        """Ask the console where the agent's chain is, at most every
        `ASK_HEAD_SECONDS`."""
        now = self._clock()
        if self._asked is not None and now - self._asked < ASK_HEAD_SECONDS:
            return
        self._asked = now
        try:
            await self._send({"type": "audit_head", "agent_id": self.agent_id})
        except Exception as e:  # pylint: disable=broad-exception-caught  # the link dropped
            log.warn(f"audit: couldn't ask for {self.agent_id}'s chain: {e}")


def load_key() -> bytes | None:
    """The audit key, or None (logged) when records can't be encrypted here:
    no `cryptography` (`bot-bottle[remote]`) or an unreadable key file."""
    if importlib.util.find_spec("cryptography") is None:
        log.warn("audit: `cryptography` isn't installed; records aren't sent")
        return None
    try:
        return audit_key_bytes()
    except (OSError, ValueError) as e:
        log.warn(f"audit: can't use the audit key: {e}")
        return None


__all__ = ["ASK_HEAD_SECONDS", "AuditPushStore", "AuditPusher", "BATCH", "PushState", "decrypt",
           "encrypt", "load_key", "rebuilt", "record_id", "record_type"]
