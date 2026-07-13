"""Launch broker contract (PRD 0070).

A VM/container can't spawn its own host-network siblings, so the
orchestrator brokers agent launches through a small privileged component.
The request it sends is:

  * **structured** — static flags + ids only (bottle id, pool slot, a
    content-addressed image ref), never a free-form path or argv, so a
    request can't be coerced into launching an arbitrary payload; and
  * **signed** — wrapped as a compact JWS/JWT the broker verifies before
    acting, so a *compromised co-located component* (an agent-facing
    gateway, say) can't forge a launch. This is the concrete form of the
    "structured requests only" rule in the PRD security review.

Signing is **HS256** over a secret shared by the orchestrator (signer) and
the broker (verifier) — appropriate here because both are trusted host
components provisioned together; the secret is exactly what an
agent-facing component does not have. (Stdlib only — the project takes no
runtime deps; a cross-host deployment could swap in an asymmetric alg.)
"""

from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

_JWT_HEADER = {"alg": "HS256", "typ": "JWT"}
_ALLOWED_OPS = ("launch", "teardown")


class BrokerAuthError(Exception):
    """A broker request failed provenance or schema verification —
    bad/absent signature, malformed token, or a payload that doesn't match
    the fixed launch-request shape. Fail-closed: the broker must not act."""


@dataclass(frozen=True)
class LaunchRequest:
    """The structured, un-coercible launch/teardown request. Ids + flags
    only — `image_ref` is a content-addressed id, never a path/argv."""

    op: str  # one of _ALLOWED_OPS
    bottle_id: str
    source_ip: str = ""
    image_ref: str = ""
    slot: int | None = None


# --- minimal JWS/JWT (HS256), stdlib only ----------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(signing_input: str, secret: bytes) -> str:
    digest = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64url(digest)


def sign_request(req: LaunchRequest, secret: bytes) -> str:
    """Serialize `req` to signed-JWT form. Adds a per-signature `jti`
    (replay id) and `iat` (issued-at)."""
    claims: dict[str, object] = {
        "op": req.op,
        "bottle_id": req.bottle_id,
        "source_ip": req.source_ip,
        "image_ref": req.image_ref,
        "slot": req.slot,
        "jti": secrets.token_hex(8),
        "iat": int(time.time()),
    }
    header = _b64url(json.dumps(_JWT_HEADER, sort_keys=True, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}"
    return f"{signing_input}.{_mac(signing_input, secret)}"


def verify_request(token: str, secret: bytes) -> LaunchRequest:
    """Verify the signature and the request shape; return the parsed
    request. Raises `BrokerAuthError` on any failure (fail-closed)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise BrokerAuthError("malformed token")
    header_b, payload_b, sig = parts
    if not hmac.compare_digest(_mac(f"{header_b}.{payload_b}", secret), sig):
        raise BrokerAuthError("bad signature")
    try:
        header = json.loads(_b64url_decode(header_b))
        claims = json.loads(_b64url_decode(payload_b))
    except (ValueError, json.JSONDecodeError) as e:
        raise BrokerAuthError("malformed token payload") from e
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise BrokerAuthError("unexpected header/alg")
    if not isinstance(claims, dict):
        raise BrokerAuthError("claims are not an object")

    op = claims.get("op")
    bottle_id = claims.get("bottle_id")
    source_ip = claims.get("source_ip", "")
    image_ref = claims.get("image_ref", "")
    slot = claims.get("slot")
    if (
        not isinstance(op, str) or op not in _ALLOWED_OPS
        or not isinstance(bottle_id, str) or not bottle_id
        or not isinstance(source_ip, str) or not isinstance(image_ref, str)
        or not (slot is None or isinstance(slot, int))
    ):
        raise BrokerAuthError("request does not match the launch-request schema")
    return LaunchRequest(
        op=op, bottle_id=bottle_id, source_ip=source_ip, image_ref=image_ref, slot=slot
    )


# --- the broker itself ------------------------------------------------------

class LaunchBroker(abc.ABC):
    """Verifies a signed request came from the orchestrator, then performs
    the backend-native launch/teardown. Subclasses implement `_launch` /
    `_teardown`; verification is shared and fail-closed."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def submit(self, token: str) -> LaunchRequest:
        """Verify `token` and perform its op. Returns the verified request;
        raises `BrokerAuthError` if provenance/shape fails."""
        req = verify_request(token, self._secret)
        if req.op == "launch":
            self._launch(req)
        else:
            self._teardown(req)
        return req

    @abc.abstractmethod
    def _launch(self, req: LaunchRequest) -> None:
        ...

    @abc.abstractmethod
    def _teardown(self, req: LaunchRequest) -> None:
        ...


class StubBroker(LaunchBroker):
    """Dev-harness broker: records verified requests without launching
    anything. Exercises the full sign -> verify -> act contract in-process."""

    def __init__(self, secret: bytes) -> None:
        super().__init__(secret)
        self.launched: list[LaunchRequest] = []
        self.torn_down: list[LaunchRequest] = []

    def _launch(self, req: LaunchRequest) -> None:
        self.launched.append(req)

    def _teardown(self, req: LaunchRequest) -> None:
        self.torn_down.append(req)


__all__ = [
    "BrokerAuthError",
    "LaunchRequest",
    "LaunchBroker",
    "StubBroker",
    "sign_request",
    "verify_request",
]
