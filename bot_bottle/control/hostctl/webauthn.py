"""A WebAuthn assertion, verified on the host (ADR 0013).

The host unlocks reading its audit records only for an assertion made by a
passkey it trusts, so a console (compromised or not) can't read them by
asking. This is the relying party's half of `navigator.credentials.get`:
the authenticator data and client data the browser returns, checked against
the passkey's public key (its COSE key, from pairing), the console's origin
and rp id, and a challenge this host issued.

Only what that needs: a CBOR decoder for COSE keys (maps of ints, byte and
text strings), ES256 (P-256) and EdDSA (Ed25519). `cryptography` is imported
when a signature is checked (`bot-bottle[remote]`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from typing import Any

ES256 = -7
EDDSA = -8

FLAG_UP = 0x01  # user present
FLAG_UV = 0x04  # user verified


class WebAuthnError(Exception):
    """Why an assertion isn't accepted; the message may reach the console."""


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --- CBOR (RFC 8949), just enough for a COSE key ---

def _cbor_item(data: bytes, i: int) -> tuple[Any, int]:
    if i >= len(data):
        raise WebAuthnError("truncated CBOR")
    major, info = data[i] >> 5, data[i] & 0x1F
    i += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        if i + n > len(data):
            raise WebAuthnError("truncated CBOR")
        arg = int.from_bytes(data[i:i + n], "big")
        i += n
    else:
        raise WebAuthnError("unsupported CBOR")
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major in (2, 3):
        if i + arg > len(data):
            raise WebAuthnError("truncated CBOR")
        raw = data[i:i + arg]
        return (raw if major == 2 else raw.decode("utf-8")), i + arg
    if major == 5:
        out: dict[Any, Any] = {}
        for _ in range(arg):
            k, i = _cbor_item(data, i)
            v, i = _cbor_item(data, i)
            out[k] = v
        return out, i
    raise WebAuthnError("unsupported CBOR")


def cose_key(data: bytes) -> dict[Any, Any]:
    """A COSE key's map, as the passkey's `public_key` holds it."""
    key, _ = _cbor_item(data, 0)
    if not isinstance(key, dict):
        raise WebAuthnError("a COSE key is a map")
    return key


def fingerprint(public_key: bytes) -> str:
    """What the user compares between `bot-bottle pair` and the console:
    the first 16 bytes of the COSE key's SHA-256, in groups of four hex."""
    digest = hashlib.sha256(public_key).hexdigest()[:32]
    return " ".join(digest[i:i + 4] for i in range(0, 32, 4))


def key_alg(public_key: bytes) -> int:
    alg = cose_key(public_key).get(3)
    if alg not in (ES256, EDDSA):
        raise WebAuthnError(f"unsupported passkey algorithm {alg!r}")
    return int(alg)


def _verify_signature(public_key: bytes, signature: bytes, signed: bytes) -> None:
    # pylint: disable=import-outside-toplevel
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    key = cose_key(public_key)
    alg = key.get(3)
    try:
        if alg == ES256:
            if key.get(1) != 2 or key.get(-1) != 1:  # kty EC2, crv P-256
                raise WebAuthnError("an ES256 passkey is an EC2 P-256 key")
            x, y = key.get(-2), key.get(-3)
            if not (isinstance(x, bytes) and isinstance(y, bytes)):
                raise WebAuthnError("malformed EC2 key")
            pub = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
            pub.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == EDDSA:
            if key.get(1) != 1 or key.get(-1) != 6:  # kty OKP, crv Ed25519
                raise WebAuthnError("an EdDSA passkey is an OKP Ed25519 key")
            x = key.get(-2)
            if not isinstance(x, bytes):
                raise WebAuthnError("malformed OKP key")
            ed25519.Ed25519PublicKey.from_public_bytes(x).verify(signature, signed)
        else:
            raise WebAuthnError(f"unsupported passkey algorithm {alg!r}")
    except InvalidSignature as e:
        raise WebAuthnError("the passkey's signature doesn't verify") from e
    except ValueError as e:
        raise WebAuthnError("malformed passkey key") from e


@dataclass(frozen=True)
class Verified:
    credential_id: str
    sign_count: int
    user_verified: bool


def verify_assertion(assertion: dict[str, Any], *, public_key: bytes, credential_id: str,
                     challenge: bytes, rp_id: str, origin: str, stored_sign_count: int,
                     require_uv: bool = False) -> Verified:
    """Check a `navigator.credentials.get` result (its `id` and `response`:
    `authenticatorData`, `clientDataJSON`, `signature`, base64url) against
    the trusted passkey and this host's challenge. WebAuthnError if not."""
    if assertion.get("id") != credential_id:
        raise WebAuthnError("the assertion is from another passkey")
    response = assertion.get("response")
    if not isinstance(response, dict):
        raise WebAuthnError("the assertion has no response")
    try:
        auth_data = b64url_decode(str(response["authenticatorData"]))
        client_json = b64url_decode(str(response["clientDataJSON"]))
        signature = b64url_decode(str(response["signature"]))
    except (KeyError, ValueError) as e:
        raise WebAuthnError("malformed assertion") from e

    try:
        client = json.loads(client_json)
    except ValueError as e:
        raise WebAuthnError("malformed client data") from e
    if not isinstance(client, dict) or client.get("type") != "webauthn.get":
        raise WebAuthnError("not a passkey sign-in")
    try:
        got_challenge = b64url_decode(str(client.get("challenge", "")))
    except ValueError as e:
        raise WebAuthnError("malformed challenge") from e
    if not hmac.compare_digest(got_challenge, challenge):
        raise WebAuthnError("the challenge isn't this host's")
    if client.get("origin") != origin:
        raise WebAuthnError(f"signed for {client.get('origin')!r}, not {origin!r}")
    if client.get("crossOrigin"):
        raise WebAuthnError("a cross-origin sign-in isn't accepted")

    if len(auth_data) < 37:
        raise WebAuthnError("authenticator data too short")
    if not hmac.compare_digest(auth_data[:32], hashlib.sha256(rp_id.encode()).digest()):
        raise WebAuthnError(f"signed for another site than {rp_id}")
    flags = auth_data[32]
    if not flags & FLAG_UP:
        raise WebAuthnError("the user wasn't present")
    if require_uv and not flags & FLAG_UV:
        raise WebAuthnError("the user wasn't verified")
    (sign_count,) = struct.unpack(">I", auth_data[33:37])
    # A counter that doesn't move on (both 0: an authenticator without one)
    # may be a cloned passkey.
    if (sign_count or stored_sign_count) and sign_count <= stored_sign_count:
        raise WebAuthnError("the passkey's counter went backwards (a cloned passkey?)")

    _verify_signature(public_key, signature, auth_data + hashlib.sha256(client_json).digest())
    return Verified(credential_id, sign_count, bool(flags & FLAG_UV))


__all__ = ["EDDSA", "ES256", "Verified", "WebAuthnError", "b64url_decode", "b64url_encode",
           "cose_key", "fingerprint", "key_alg", "verify_assertion"]
