"""Role-scoped control-plane credentials (issue #469 review follow-up).

The control plane no longer trusts a single shared bearer secret for every
route. Instead the orchestrator holds a *signing key* and issues short,
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
    the orchestrator's key — see `trust_domain.py`, issues #476/#468.

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


__all__ = ["ROLE_GATEWAY", "ROLE_CLI", "ROLES", "mint", "verify"]
