"""Encryption of per-bottle egress secrets at rest.

Each agent gets a random ENV_VAR_SECRET in its env, never logged or stored.
Tokens are encrypted with it into `bottled_agent_secrets`, so the rows alone
recover nothing. After an orchestrator restart the host reads the secret from
the running agent and posts it to `/bottles/<id>/reprovision_gateway`.

Encrypt-then-MAC with HMAC-SHA256-derived keys (stdlib only):

    version || nonce (16 bytes) || ciphertext || tag (32 bytes)

as URL-safe base64. Unversioned legacy blobs are rejected.

    keystream_block_i = HMAC-SHA256(key, nonce || i.to_bytes(4, "big"))
    ciphertext_i      = plaintext_i XOR keystream_block_i[:len(plaintext_i)]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_KEY_BYTES = 32  # 256-bit key
_NONCE_BYTES = 16  # per-encryption random nonce
_BLOCK = 32  # one keystream block
_TAG_BYTES = 32
_VERSION = b"BBSE1"

# The agent's env var name for it.
ENV_VAR_SECRET_NAME = "ENV_VAR_SECRET"


def new_env_var_secret() -> str:
    """A fresh ENV_VAR_SECRET: 32 random bytes, URL-safe base64."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).rstrip(b"=").decode()


def _b64dec(s: str) -> bytes:
    return base64.b64decode(
        s + "=" * (-len(s) % 4), altchars=b"-_", validate=True,
    )


def _subkey(key: bytes, purpose: bytes) -> bytes:
    return hmac.new(key, b"bot-bottle-secret-store:" + purpose, hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, block_index: int) -> bytes:
    return hmac.new(
        key, nonce + block_index.to_bytes(4, "big"), hashlib.sha256
    ).digest()


def encrypt_value(secret_b64: str, plaintext: str) -> str:
    """Encrypt `value` with `secret_b64`; returns a URL-safe base64 blob."""
    key = _b64dec(secret_b64)
    encryption_key = _subkey(key, b"encryption")
    authentication_key = _subkey(key, b"authentication")
    pt = plaintext.encode()
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ct = bytearray()
    for i in range(0, len(pt), _BLOCK):
        chunk = pt[i : i + _BLOCK]
        ks = _keystream(encryption_key, nonce, i // _BLOCK)[: len(chunk)]
        ct.extend(p ^ k for p, k in zip(chunk, ks))
    authenticated = _VERSION + nonce + bytes(ct)
    tag = hmac.new(authentication_key, authenticated, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(authenticated + tag).rstrip(b"=").decode()


def decrypt_value(secret_b64: str, blob_b64: str) -> str:
    """Decrypt an `encrypt_value` blob. Raises ValueError when malformed,
    unauthenticated or under the wrong key."""
    key = _b64dec(secret_b64)
    try:
        blob = _b64dec(blob_b64)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid ciphertext blob: {exc}") from exc
    if not blob.startswith(_VERSION):
        raise ValueError("unsupported ciphertext format")
    minimum = len(_VERSION) + _NONCE_BYTES + _TAG_BYTES
    if len(blob) < minimum:
        raise ValueError("ciphertext blob too short")
    authenticated, supplied_tag = blob[:-_TAG_BYTES], blob[-_TAG_BYTES:]
    authentication_key = _subkey(key, b"authentication")
    expected_tag = hmac.new(
        authentication_key, authenticated, hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_tag, expected_tag):
        raise ValueError("ciphertext authentication failed")
    nonce_start = len(_VERSION)
    nonce = blob[nonce_start : nonce_start + _NONCE_BYTES]
    ciphertext = blob[nonce_start + _NONCE_BYTES : -_TAG_BYTES]
    encryption_key = _subkey(key, b"encryption")
    pt = bytearray()
    for i in range(0, len(ciphertext), _BLOCK):
        chunk = ciphertext[i : i + _BLOCK]
        ks = _keystream(encryption_key, nonce, i // _BLOCK)[: len(chunk)]
        pt.extend(c ^ k for c, k in zip(chunk, ks))
    try:
        return bytes(pt).decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"decryption produced non-UTF-8 output (wrong key?): {exc}") from exc


__all__ = [
    "ENV_VAR_SECRET_NAME",
    "new_env_var_secret",
    "encrypt_value",
    "decrypt_value",
]
