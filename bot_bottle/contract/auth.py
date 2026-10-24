"""Role-scoped control-plane credentials: compact HS256 JWTs carrying a role,
stdlib only.

- `gateway`: the data plane, limited to `/resolve`, `/supervise/propose` and
  `/supervise/poll`.
- `cli`: the host operator, with full access.

Only the key holder can mint; the gateway gets a `gateway` token it can't
rewrite, so a compromised data plane can't approve its own proposals. Tokens
are signed, not encrypted, and long-lived.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from .identity import ORCHESTRATOR_AUTH_JWT_ENV, ORCHESTRATOR_TOKEN_ENV

ROLE_GATEWAY = "gateway"
ROLE_CLI = "cli"
ROLES: frozenset[str] = frozenset({ROLE_GATEWAY, ROLE_CLI})

_ALG = "HS256"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign(secret: str, signing_input: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return _b64url_encode(mac.digest())


# The fixed JOSE header every token uses.
_HEADER_SEGMENT = _b64url_encode(
    json.dumps({"alg": _ALG, "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
)


def mint(role: str, secret: str, *, roles: frozenset[str] = ROLES) -> str:
    """A compact HS256 token asserting `role`, signed with `secret`. `roles` is
    the set the key may sign (default `{gateway, cli}`). Raises ValueError for
    a role outside it or an empty key."""
    if role not in roles:
        raise ValueError(f"unknown control-plane role {role!r}")
    if not secret:
        raise ValueError("cannot mint a control-plane token without a signing key")
    payload = _b64url_encode(json.dumps({"role": role}, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{_HEADER_SEGMENT}.{payload}"
    return f"{signing_input}.{_sign(secret, signing_input)}"


def verify(token: str, secret: str, *, roles: frozenset[str] = ROLES) -> str | None:
    """The role a valid `token` carries, or None if malformed, wrongly signed,
    or outside `roles`. Constant-time; any alg but HS256 is rejected."""
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig = parts
    expected = _sign(secret, f"{header_b64}.{payload_b64}")
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(header, dict) or header.get("alg") != _ALG:
        return None
    role = payload.get("role") if isinstance(payload, dict) else None
    return role if isinstance(role, str) and role in roles else None


@dataclass(frozen=True)
class TrustDomain:
    """One service's signing material: its key file name, the roles it may
    sign, and the env vars carrying the key (`key_env`, for the owner) and a
    minted token (`token_env`, for a delegate). Reading the key file is
    host-side (`bot_bottle.trust_domain`)."""

    name: str
    key_filename: str
    roles: frozenset[str]
    key_env: str
    token_env: str

    def key_from_env(self, environ: Mapping[str, str] | None = None) -> str:
        """The key from `key_env` (default `os.environ`), or "". Owners refuse
        to start without one."""
        env = os.environ if environ is None else environ
        return env.get(self.key_env, "").strip()

    def mint(self, role: str, key: str) -> str:
        """A role-scoped token signed with `key`. Raises ValueError for a role
        this domain doesn't sign."""
        if role not in self.roles:
            raise ValueError(f"role {role!r} is not in trust domain {self.name!r}")
        return mint(role, key, roles=self.roles)

    def verify(self, token: str, key: str) -> str | None:
        """The role `token` carries under `key`, or None."""
        return verify(token, key, roles=self.roles)


# The orchestrator's key file under the host root.
ORCHESTRATOR_TOKEN_FILENAME = "orchestrator-token"

# The orchestrator's domain: it and the host CLI hold the key; it mints the
# gateway's token and the CLI mints its own.
CONTROL_PLANE = TrustDomain(
    name="control-plane",
    key_filename=ORCHESTRATOR_TOKEN_FILENAME,
    roles=ROLES,
    key_env=ORCHESTRATOR_TOKEN_ENV,
    token_env=ORCHESTRATOR_AUTH_JWT_ENV,
)


__all__ = [
    "ROLE_GATEWAY",
    "ROLE_CLI",
    "ROLES",
    "mint",
    "verify",
    "TrustDomain",
    "ORCHESTRATOR_TOKEN_FILENAME",
    "CONTROL_PLANE",
]
