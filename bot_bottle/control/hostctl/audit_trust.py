"""The passkeys this host trusts to unlock reading its audit records (ADR 0013).

Fetched from the console at pairing (`bot-bottle pair`) or with
`bot-bottle audit passkeys --trust`, and trusted only once the user confirms
the fingerprints match what the console shows: a console compromised before
pairing could otherwise hand over its own key. Changing the trusted set is a
local CLI action, never a console request. Kept in `<root>/audit-passkeys.json`
(0600) with the console's origin and rp id they sign for, and each passkey's
last signature counter.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ...paths import audit_passkeys_path
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


class TrustError(Exception):
    """Why passkeys couldn't be fetched or trusted."""


def origin_and_rp_id(console_url: str) -> tuple[str, str]:
    """The origin a passkey signs for on the console at `console_url`, and its rp id."""
    parts = urlsplit(console_url)
    if parts.scheme != "https" or not parts.hostname:
        raise TrustError(f"{console_url} isn't an https console URL")
    return f"{parts.scheme}://{parts.netloc}", parts.hostname


def load(path: Path | None = None) -> Trust:
    path = path or audit_passkeys_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return Trust()
    except (OSError, ValueError) as e:
        raise TrustError(f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise TrustError(f"{path} isn't a passkey list")
    keys = [TrustedPasskey(id=str(p["id"]), public_key=str(p["public_key"]),
                           sign_count=int(p.get("sign_count", 0)))
            for p in raw.get("passkeys", []) if isinstance(p, dict) and "id" in p
            and "public_key" in p]
    return Trust(origin=str(raw.get("origin", "")), rp_id=str(raw.get("rp_id", "")),
                 passkeys=keys)


def save(trust: Trust, path: Path | None = None) -> None:
    path = path or audit_passkeys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"origin": trust.origin, "rp_id": trust.rp_id,
                   "passkeys": [asdict(p) for p in trust.passkeys]}, f, indent=2)
    os.replace(tmp, path)


def record_sign_count(credential_id: str, sign_count: int, path: Path | None = None) -> None:
    """Keep a passkey's latest counter, so a replayed or cloned one is caught."""
    trust = load(path)
    key = trust.find(credential_id)
    if key is not None and sign_count > key.sign_count:
        key.sign_count = sign_count
        save(trust, path)


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


def trusted(console_url: str, fetched: list[TrustedPasskey], current: Trust) -> Trust:
    """The trust to save once the user confirmed `fetched`: their counters kept
    where a passkey was already trusted."""
    origin, rp_id = origin_and_rp_id(console_url)
    kept = []
    for p in fetched:
        old = current.find(p.id)
        if old is not None and old.public_key == p.public_key:
            p.sign_count = old.sign_count
        kept.append(p)
    return Trust(origin=origin, rp_id=rp_id, passkeys=kept)


def describe(keys: list[TrustedPasskey]) -> list[str]:
    return [f"  {p.fingerprint}" for p in keys]


__all__ = ["Trust", "TrustError", "TrustedPasskey", "describe", "fetch", "load",
           "origin_and_rp_id", "record_sign_count", "save", "trusted"]
