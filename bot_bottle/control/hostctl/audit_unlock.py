"""Unlocking audit reads with a passkey, once per console session (ADR 0013).

The console asks for a challenge (single use, two minutes), the user signs it
with a passkey this host trusts, and the host checks the signature itself
(`webauthn`). That issues an unlock token bound to the console session it
was asked for: good for 12 hours, or an hour without use. Reads with it are
rate-limited. All in memory: a restarted host controller asks again.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import audit_trust
from .webauthn import WebAuthnError, b64url_decode, b64url_encode, verify_assertion

CHALLENGE_SECONDS = 120.0
TOKEN_MAX_SECONDS = 12 * 3600.0
TOKEN_IDLE_SECONDS = 3600.0
# Records a token may decrypt per minute (a run's history is read a page at a time).
READS_PER_MINUTE = 2000


class UnlockError(Exception):
    """Why an unlock or a read was refused; the message reaches the console."""


@dataclass
class _Token:
    session: str
    credential_id: str
    issued: float
    used: float
    reads: list[float] = field(default_factory=list)


class AuditUnlock:
    def __init__(self, db_path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        # The host DB, with the trusted passkeys (`audit_trust`).
        self._db_path = db_path
        self._clock = clock
        self._lock = threading.Lock()
        self._challenges: dict[str, tuple[float, str]] = {}  # nonce -> (expires, session)
        self._tokens: dict[str, _Token] = {}

    def challenge(self, session: str) -> dict[str, object]:
        """A challenge for the console session `session` to sign, with what
        the browser needs to ask for it."""
        if not session:
            raise UnlockError("no console session")
        trust = audit_trust.load(self._db_path)
        if not trust.passkeys:
            raise UnlockError("this host trusts no passkey yet: run "
                              "`bot-bottle audit passkeys --trust` on it")
        nonce = secrets.token_bytes(32)
        now = self._clock()
        with self._lock:
            self._challenges = {n: v for n, v in self._challenges.items() if v[0] > now}
            self._challenges[b64url_encode(nonce)] = (now + CHALLENGE_SECONDS, session)
        return {"challenge": b64url_encode(nonce), "rp_id": trust.rp_id,
                "credential_ids": [p.id for p in trust.passkeys],
                "expires_in": int(CHALLENGE_SECONDS)}

    def unlock(self, session: str, assertion: dict[str, object]) -> dict[str, object]:
        """Verify `assertion` (signed over a challenge this host issued for
        `session`) and issue an unlock token for that session."""
        response = assertion.get("response")
        client = response.get("clientDataJSON") if isinstance(response, dict) else None
        nonce = challenge_of(str(client or ""))
        now = self._clock()
        with self._lock:
            issued = self._challenges.pop(nonce, None)  # single use, even if it fails below
        if issued is None or issued[0] <= now:
            raise UnlockError("that challenge expired or was used; try again")
        if issued[1] != session:
            raise UnlockError("that challenge was issued to another session")
        credential = verify_passkey(assertion, nonce, self._db_path)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = _Token(session, credential, now, now)
        return {"token": token, "expires_in": int(TOKEN_MAX_SECONDS),
                "idle_seconds": int(TOKEN_IDLE_SECONDS)}

    def check(self, token: str, session: str, reads: int = 1) -> str:
        """The passkey that unlocked `token` for `session`, counting `reads`
        records against its rate; UnlockError if it can't read now."""
        now = self._clock()
        with self._lock:
            t = self._tokens.get(token)
            if t is None or now - t.issued > TOKEN_MAX_SECONDS or now - t.used > TOKEN_IDLE_SECONDS:
                self._tokens.pop(token, None)
                raise UnlockError("unlock with your passkey")
            if t.session != session:
                raise UnlockError("unlock with your passkey")
            t.reads = [r for r in t.reads if now - r < 60.0]
            if len(t.reads) + reads > READS_PER_MINUTE:
                raise UnlockError("too many reads; wait a minute")
            t.reads.extend([now] * reads)
            t.used = now
            return t.credential_id


def verify_passkey(assertion: dict[str, object], nonce: str, db_path: Path | None = None) -> str:
    """Check `assertion` is a trusted passkey's signature over the challenge
    `nonce` (base64url) this host issued: the passkey's id, or UnlockError.
    Shared by every action a passkey authorizes (audit reads, upgrades)."""
    trust = audit_trust.load(db_path)
    key = trust.find(str(assertion.get("id", "")))
    if key is None:
        raise UnlockError("that passkey isn't trusted by this host")
    try:
        verified = verify_assertion(
            assertion, public_key=key.key_bytes, credential_id=key.id,
            challenge=b64url_decode(nonce), rp_id=trust.rp_id, origin=trust.origin,
            stored_sign_count=key.sign_count)
    except WebAuthnError as e:
        raise UnlockError(str(e)) from e
    audit_trust.record_sign_count(key.id, verified.sign_count, db_path)
    return key.id


def challenge_of(client_data_b64: str) -> str:
    """The challenge a client data JSON carries ("" if it doesn't)."""
    try:
        return str(json.loads(b64url_decode(client_data_b64)).get("challenge", ""))
    except (ValueError, AttributeError):
        return ""


__all__ = ["AuditUnlock", "UnlockError", "READS_PER_MINUTE", "TOKEN_IDLE_SECONDS",
           "TOKEN_MAX_SECONDS", "challenge_of", "verify_passkey"]
