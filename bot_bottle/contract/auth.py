"""Role-scoped control-plane credentials.

The control plane does not trust a single shared bearer secret for every
route. The orchestrator holds a *signing key* and issues short,
HMAC-signed tokens (compact HS256 JWTs) that embed a **role** naming the kind
of caller:

  * ``gateway`` — the data plane (egress / git-gate / supervise). Restricted to
    the agent-facing routes it actually needs (``/resolve``,
    ``/supervise/propose``, ``/supervise/poll``).
  * ``cli`` — the host operator / launcher. Full access to the mutating and
    operator routes (launch/teardown, policy, ``/supervise/respond``, …).

Only the orchestrator (and the host CLI, which shares the host trust domain)
holds the signing key; the gateway is handed a pre-minted ``gateway`` token it
cannot rewrite into a ``cli`` token. So a compromised data-plane process can no
longer approve its own supervise proposals or drive operator routes — it can
only present the ``gateway`` role it was issued (the control plane rejects it
on operator routes with 403).

Stdlib-only (HMAC-SHA256 over a JSON payload); no JWT dependency — the project
carries no runtime pip deps. Tokens are **signed, not encrypted** (the role is
not a secret) and **long-lived** (parity with the static token they replace;
the security win is the unforgeable role claim, not rotation).
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


# The fixed, canonical JOSE header — the same for every token we mint.
_HEADER_SEGMENT = _b64url_encode(
    json.dumps({"alg": _ALG, "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
)


def mint(role: str, secret: str, *, roles: frozenset[str] = ROLES) -> str:
    """A compact HS256 token asserting `role`, signed with `secret`.

    `roles` is the set the signing key is allowed to sign (default: the
    orchestrator's `{gateway, cli}`). A separate service (e.g. the host
    controller) passes its own key + role set so its tokens can't be forged with
    the orchestrator's key — see `TrustDomain`.

    Raises ValueError for a role outside `roles`, or an empty signing key (an
    unsigned credential is never valid)."""
    if role not in roles:
        raise ValueError(f"unknown control-plane role {role!r}")
    if not secret:
        raise ValueError("cannot mint a control-plane token without a signing key")
    payload = _b64url_encode(json.dumps({"role": role}, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{_HEADER_SEGMENT}.{payload}"
    return f"{signing_input}.{_sign(secret, signing_input)}"


def verify(token: str, secret: str, *, roles: frozenset[str] = ROLES) -> str | None:
    """The role a valid `token` carries, or None if it is malformed, wrongly
    signed, or names a role outside `roles` (the verifying trust domain's set —
    default `{gateway, cli}`). Constant-time signature check; rejects any header
    whose alg isn't HS256 (no alg-confusion / `none`)."""
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
    """One service's signing material: the name of its host-canonical key file,
    the roles that key may sign, and the env vars its key and a minted token
    ride in.

    The service that owns the domain (e.g. the orchestrator) receives the raw
    key via `key_env`; a delegate (e.g. the gateway) receives only a pre-minted,
    role-scoped token via `token_env`. `mint`/`verify` are scoped to `roles`, so
    one service's key can neither sign nor accept another service's role.
    Reading the key file is host-side (`bot_bottle.trust_domain`)."""

    name: str
    key_filename: str
    roles: frozenset[str]
    key_env: str
    token_env: str

    def key_from_env(self, environ: Mapping[str, str] | None = None) -> str:
        """The signing key as the owning process sees it, read from `key_env`
        (default `os.environ`). ``""`` when unset; owning services reject that
        value rather than start without authentication."""
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


# The orchestrator's key file under the host app-data root.
ORCHESTRATOR_TOKEN_FILENAME = "orchestrator-token"

# The orchestrator's domain: the key the orchestrator (and host CLI) holds, the
# `gateway` token it mints for the data plane, and the `cli` token the CLI mints
# for itself.
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
