"""The passkeys this host trusts to unlock reading its audit records (ADR 0013).

Fetched from the console at pairing (`bot-bottle pair`) or with
`bot-bottle audit passkeys --trust`, and trusted only once the user confirms
the fingerprints match what the console shows: a console compromised before
pairing could otherwise hand over its own key. Changing the trusted set is a
local CLI action, never a console request. Kept in the host DB's
`audit_passkeys` (ADR 0020, portable) with the console's origin and rp id they
sign for, and each passkey's last signature counter. The host controller is
its only writer; the CLI goes through `AUDIT_PASSKEYS_PATH`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ...lib.db_store import PORTABLE, DbStore
from ...lib.migrations import TableMigrations
from ...paths import host_db_path
from .webauthn import WebAuthnError, b64url_decode, fingerprint, key_alg


@dataclass
class TrustedPasskey:
    id: str                 # base64url credential id
    public_key: str         # base64url COSE key
    sign_count: int = 0

    @property
    def key_bytes(self) -> bytes:
        return b64url_decode(self.public_key)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.key_bytes)


@dataclass
class Trust:
    origin: str = ""
    rp_id: str = ""
    passkeys: list[TrustedPasskey] = field(default_factory=list)

    def find(self, credential_id: str) -> TrustedPasskey | None:
        return next((p for p in self.passkeys if p.id == credential_id), None)

    def to_json(self) -> dict[str, object]:
        return {"origin": self.origin, "rp_id": self.rp_id,
                "passkeys": [asdict(p) for p in self.passkeys]}

    @classmethod
    def from_json(cls, raw: object) -> "Trust":
        """`to_json`'s shape; TrustError if it isn't."""
        if not isinstance(raw, dict) or not isinstance(raw.get("passkeys", []), list):
            raise TrustError("a passkey list is {origin, rp_id, passkeys: [...]}")
        keys = []
        for p in raw.get("passkeys", []):
            if not isinstance(p, dict) or not isinstance(p.get("id"), str) \
                    or not isinstance(p.get("public_key"), str):
                raise TrustError("each passkey has an id and a public_key")
            count = p.get("sign_count", 0)
            keys.append(TrustedPasskey(id=p["id"], public_key=p["public_key"],
                                       sign_count=count if isinstance(count, int) else 0))
        return cls(origin=str(raw.get("origin", "")), rp_id=str(raw.get("rp_id", "")),
                   passkeys=keys)


class TrustError(Exception):
    """Why passkeys couldn't be fetched or trusted."""


def origin_and_rp_id(console_url: str) -> tuple[str, str]:
    """The origin a passkey signs for on the console at `console_url`, and its rp id."""
    parts = urlsplit(console_url)
    if parts.scheme != "https" or not parts.hostname:
        raise TrustError(f"{console_url} isn't an https console URL")
    return f"{parts.scheme}://{parts.netloc}", parts.hostname


class PasskeyStore(DbStore):
    """The host DB's trusted audit passkeys. Only the host controller opens
    it."""

    # The account's passkeys, and their counters, travel with its owner.
    TABLES = {"audit_passkeys": PORTABLE}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("audit_passkeys", [
            # Every row carries the one origin and rp id the set signs for.
            """
            CREATE TABLE audit_passkeys (
                id TEXT PRIMARY KEY,
                public_key TEXT NOT NULL,
                sign_count INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL,
                rp_id TEXT NOT NULL
            )
            """,
        ]))
        self.migrate()

    def trust(self) -> Trust:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM audit_passkeys ORDER BY rowid").fetchall()
        if not rows:
            return Trust()
        return Trust(origin=rows[0]["origin"], rp_id=rows[0]["rp_id"], passkeys=[
            TrustedPasskey(id=r["id"], public_key=r["public_key"], sign_count=r["sign_count"])
            for r in rows])

    def replace(self, trust: Trust) -> Trust:
        """Trust exactly `trust`'s passkeys, keeping each one's counter where
        the same key was trusted already, in one transaction."""
        with self._write_connection() as conn:
            counts = {(r["id"], r["public_key"]): r["sign_count"]
                      for r in conn.execute("SELECT * FROM audit_passkeys")}
            conn.execute("DELETE FROM audit_passkeys")
            for p in trust.passkeys:
                conn.execute("INSERT INTO audit_passkeys VALUES (?, ?, ?, ?, ?)",
                             (p.id, p.public_key, counts.get((p.id, p.public_key), 0),
                              trust.origin, trust.rp_id))
        self._chmod()
        return self.trust()

    def record_sign_count(self, credential_id: str, sign_count: int) -> None:
        """Keep a passkey's latest counter, so a replayed or cloned one is
        caught."""
        with self._connection() as conn:
            conn.execute("UPDATE audit_passkeys SET sign_count = ? "
                         "WHERE id = ? AND sign_count < ?",
                         (sign_count, credential_id, sign_count))
        self._chmod()


def load(db_path: Path | None = None) -> Trust:
    return PasskeyStore(db_path or host_db_path()).trust()


def record_sign_count(credential_id: str, sign_count: int,
                      db_path: Path | None = None) -> None:
    PasskeyStore(db_path or host_db_path()).record_sign_count(credential_id, sign_count)


def fetch(console_url: str, access_token: str, *, timeout: float = 10.0) -> list[TrustedPasskey]:
    """The account's passkeys as the console lists them for this host
    (`GET /api/v1/hosts/passkeys`). Not trusted until the user confirms."""
    req = urllib.request.Request(f"{console_url.rstrip('/')}/api/v1/hosts/passkeys",
                                 headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        raise TrustError(f"the console refused ({e.code}); is this host still paired?") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TrustError(f"couldn't reach the console: {e}") from e
    out: list[TrustedPasskey] = []
    for p in body.get("passkeys", []) if isinstance(body, dict) else []:
        try:
            key = TrustedPasskey(id=str(p["id"]), public_key=str(p["public_key"]))
            key_alg(key.key_bytes)
        except (KeyError, TypeError, ValueError, WebAuthnError):
            continue  # one this host can't verify is left out
        out.append(key)
    return out


def trusted(console_url: str, fetched: list[TrustedPasskey]) -> Trust:
    """The trust to keep once the user confirmed `fetched` (`PasskeyStore`
    keeps a counter where a passkey was already trusted)."""
    origin, rp_id = origin_and_rp_id(console_url)
    return Trust(origin=origin, rp_id=rp_id, passkeys=list(fetched))


def describe(keys: list[TrustedPasskey]) -> list[str]:
    return [f"  {p.fingerprint}" for p in keys]


__all__ = ["PasskeyStore", "Trust", "TrustError", "TrustedPasskey", "describe", "fetch",
           "load", "origin_and_rp_id", "record_sign_count", "trusted"]
