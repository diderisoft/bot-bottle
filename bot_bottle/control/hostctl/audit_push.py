"""Audit records: each console agent's conversation, encrypted on the host and
sent to the console, which keeps only ciphertext (ADR 0013).

One `AuditPusher` per agent. Each entry of its conversation (as the host's
transcript copy holds it, masked: `transcript_serve`) is encrypted with the
host's AES-256-GCM audit key (`<root>/audit.key`, never sent), bound to its
run and position, and hash-chained to the last record sent for the agent, so
the chain carries on across its runs. How far it got is kept in
`<root>/audit-push/<agent>.json`: a restarted host controller, a resume or a
re-adopted bottle carries on there, never sending an entry twice. A tick sends
at most `BATCH` records, off the event loop but for the sends themselves.

Reading them back is `decrypt`: only for a run this host sent, and only once
the user unlocked with a passkey (`audit_unlock`).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib import log
from ...paths import audit_key_bytes, audit_push_dir

if TYPE_CHECKING:
    from ...transcript import TranscriptEntry

BATCH = 50
_NS = uuid.UUID("6f0b6e8c-6c1f-4a8e-9b1a-2f6a6b0c7e51")
_RECORD_TYPES = {"user": "user_message", "assistant": "assistant_message",
                 "tool_call": "tool_call", "tool_result": "tool_result", "system": "system_event"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


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


def _state_path(directory: Path, agent_id: str) -> Path:
    return directory / f"{_SAFE.sub('_', agent_id)}.json"


def load_state(directory: Path, agent_id: str) -> PushState:
    path = _state_path(directory, agent_id)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return PushState()
    except (OSError, ValueError) as e:
        log.warn(f"audit: {path} is unreadable ({e}); starting its records over")
        return PushState()
    last = raw.get("last_entry_seq")
    return PushState(last_entry_seq=last if isinstance(last, int) else None,
                     chain_hash=str(raw.get("chain_hash", "")),
                     runs={str(k): int(v) for k, v in dict(raw.get("runs", {})).items()})


def save_state(directory: Path, agent_id: str, state: PushState) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = _state_path(directory, agent_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"last_entry_seq": state.last_entry_seq,
                               "chain_hash": state.chain_hash, "runs": state.runs}))
    os.replace(tmp, path)


def host_runs(directory: Path) -> set[str]:
    """Every run this host has sent records for."""
    out: set[str] = set()
    for path in directory.glob("*.json") if directory.is_dir() else []:
        try:
            out.update(dict(json.loads(path.read_text()).get("runs", {})))
        except (OSError, ValueError, AttributeError):
            continue
    return out


class AuditPusher:
    def __init__(self, agent_id: str, key: bytes, entries: Callable[[str], list[Any] | None],
                 send: Callable[[dict[str, Any]], Awaitable[None]], *,
                 directory: Path | None = None) -> None:
        self.agent_id = agent_id
        self._key = key
        self._entries = entries
        self._send = send
        self._dir = directory or audit_push_dir()
        self._lock = threading.Lock()
        self.state = load_state(self._dir, agent_id)

    def _batch(self, run_id: str) -> list[dict[str, Any]]:
        """The next records to send, built (and the state advanced) off the loop."""
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
                self.state.last_entry_seq = entry_seq
                self.state.chain_hash = record["chain_hash"]
                self.state.runs[run_id] = record["seq"] + 1
            sent += 1
        if sent:
            await asyncio.to_thread(save_state, self._dir, self.agent_id, self.state)
        return sent


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


__all__ = ["AuditPusher", "BATCH", "PushState", "decrypt", "encrypt", "host_runs", "load_key",
           "load_state", "record_id", "record_type", "save_state"]
