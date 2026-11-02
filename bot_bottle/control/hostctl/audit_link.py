"""The console link's audit records (ADR 0013): sending them, and reading
them back once the user has unlocked with a passkey.

- `audit_unlock_challenge` (`session`) -> `audit_unlock_challenge` (`challenge`,
  `rp_id`, `credential_ids`, `expires_in`) or `error`.
- `audit_unlock` (`session`, `assertion`) -> `audit_unlocked` (`token`,
  `expires_in`, `idle_seconds`) or `error`: the host checks the passkey's
  signature itself.
- `decrypt_audit_records` (`session`, `token`, `run_id`, `records`: `id`,
  `seq`, `ciphertext`; at most 200) -> `audit_decrypted` (`records`: `id` and
  `plaintext` or `error`) or `error`. Only for a run this host sent, and each
  read is journalled (`audit.records_read`).
- `audit_record` (from the host): one encrypted record of a running console
  agent's conversation (`audit_push`), every few seconds while there are new ones.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...lib import log
from .audit_push import AuditPusher, decrypt, host_runs, load_key
from .audit_unlock import AuditUnlock, UnlockError
from .transcript_serve import TranscriptReader

PUSH_SECONDS = 5.0
MAX_RECORDS = 200


class AuditLink:
    REQUESTS = frozenset({"audit_unlock_challenge", "audit_unlock", "decrypt_audit_records"})

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 running: Callable[[], dict[str, str]], bottles: Any, *,
                 unlock: AuditUnlock | None = None, directory: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._send = send
        self._running = running
        self._bottles = bottles
        self.unlock = unlock or AuditUnlock()
        self._dir = directory
        self._clock = clock
        self._key: bytes | None = None
        self._key_tried = False
        self._pushers: dict[str, AuditPusher] = {}
        self._reader = TranscriptReader()
        self._last_push = 0.0

    def _audit_key(self) -> bytes | None:
        if not self._key_tried:
            self._key_tried = True
            self._key = load_key()
        return self._key

    # --- sending ---

    async def push(self) -> None:
        """Every PUSH_SECONDS: the next batch of each running console agent's records."""
        now = self._clock()
        if now - self._last_push < PUSH_SECONDS:
            return
        self._last_push = now
        running = self._running()
        for gone in set(self._pushers) - set(running):
            del self._pushers[gone]
        if not running or self._audit_key() is None:
            return
        runs = await asyncio.to_thread(self._activations)
        for agent_id, bottle in running.items():
            run_id = runs.get(bottle, "")
            if not run_id:
                continue
            pusher = self._pushers.get(agent_id)
            if pusher is None:
                key = self._audit_key()
                assert key is not None
                pusher = AuditPusher(agent_id, key, self._reader.entries, self._send,
                                     **({"directory": self._dir} if self._dir else {}))
                self._pushers[agent_id] = pusher
            await pusher.push(run_id)

    def _activations(self) -> dict[str, str]:
        """Each running bottle's current activation (its run id)."""
        try:
            listed = self._bottles.list()
        except Exception as e:  # pylint: disable=broad-exception-caught  # tried again next tick
            log.debug(f"audit: can't list sessions: {e}")
            return {}
        return {str(e.get("id", "")): str(e.get("activation", "")) for e in listed
                if isinstance(e, dict) and e.get("activation")}

    # --- reading back ---

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        reply: dict[str, Any] = {"type": kind, "request_id": msg.get("request_id")}
        session = str(msg.get("session") or "")
        try:
            if kind == "audit_unlock_challenge":
                reply.update(await asyncio.to_thread(self.unlock.challenge, session))
            elif kind == "audit_unlock":
                assertion = msg.get("assertion")
                if not isinstance(assertion, dict):
                    raise UnlockError("no passkey assertion")
                reply["type"] = "audit_unlocked"
                reply.update(await asyncio.to_thread(self.unlock.unlock, session, assertion))
            else:
                reply["type"] = "audit_decrypted"
                reply["records"] = await asyncio.to_thread(self._decrypt, msg, session)
        except UnlockError as e:
            reply["error"] = str(e)
        await self._send(reply)

    def _decrypt(self, msg: dict[str, Any], session: str) -> list[dict[str, Any]]:
        run_id = str(msg.get("run_id") or "")
        records = msg.get("records")
        if not isinstance(records, list) or not records or len(records) > MAX_RECORDS:
            raise UnlockError(f"send 1 to {MAX_RECORDS} records")
        credential = self.unlock.check(str(msg.get("token") or ""), session, len(records))
        if run_id not in host_runs(self._dir_path()):
            raise UnlockError("that run's records weren't sent by this host")
        key = self._audit_key()
        if key is None:
            raise UnlockError("this host can't read audit records (no audit key)")
        out: list[dict[str, Any]] = []
        for r in records:
            rid = str(r.get("id", "")) if isinstance(r, dict) else ""
            try:
                out.append({"id": rid, "plaintext": decrypt(key, run_id, int(r["seq"]),
                                                            str(r["ciphertext"]))})
            # Tampered with, or not this host's.
            except Exception:  # pylint: disable=broad-exception-caught
                out.append({"id": rid, "error": "doesn't decrypt with this host's key"})
        audit = getattr(self._bottles, "audit", None)
        if audit is not None and hasattr(audit, "audit_records_read"):
            audit.audit_records_read(run_id=run_id, count=len(out), credential_id=credential)
        return out

    def _dir_path(self) -> Any:
        if self._dir is not None:
            return self._dir
        from ...paths import audit_push_dir  # pylint: disable=import-outside-toplevel
        return audit_push_dir()


__all__ = ["AuditLink", "MAX_RECORDS", "PUSH_SECONDS"]
