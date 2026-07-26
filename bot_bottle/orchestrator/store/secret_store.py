"""Symmetric encryption for per-bottle egress secrets (PRD 0080).

Each agent receives a random ENV_VAR_SECRET at startup — passed as an env var,
never logged or persisted. The host uses this key to encrypt each egress auth
token value before writing it to the bottled_agent_secrets table; the DB rows
(ciphertext, plaintext env-var name) without the key are insufficient to
recover the credentials.

On orchestrator restart the in-memory token map is lost. The host-side
reattachment path reads ENV_VAR_SECRET from the running agent container via
``docker exec … printenv ENV_VAR_SECRET`` and posts it to
``POST /bottles/<id>/reprovision_gateway``; the orchestrator decrypts the
stored rows and re-populates ``_tokens``.

Encryption scheme: encrypt-then-MAC using independent HMAC-SHA256-derived
encryption and authentication subkeys (stdlib-only, no external deps). Each
value is encrypted independently. New output blobs are:

``version || nonce (16 bytes) || ciphertext || tag (32 bytes)``

encoded as URL-safe base64 (no padding). The version marker lets the reader
accept legacy ``nonce || ciphertext`` rows long enough to rewrite them in the
authenticated format after a successful reprovision.

    keystream_block_i = HMAC-SHA256(key, nonce || i.to_bytes(4, "big"))
    ciphertext_i      = plaintext_i XOR keystream_block_i[:len(plaintext_i)]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_KEY_BYTES = 32    # 256-bit key from ENV_VAR_SECRET
_NONCE_BYTES = 16  # 128-bit random nonce per encrypt call
_BLOCK = 32        # HMAC-SHA256 output width == one keystream block
_TAG_BYTES = 32
_VERSION = b"BBSE1"

# Env-var name the agent container receives at startup.
ENV_VAR_SECRET_NAME = "ENV_VAR_SECRET"


def new_env_var_secret() -> str:
    """Generate a fresh ENV_VAR_SECRET: 32 random bytes as URL-safe base64."""
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
    """Encrypt a single string value with *secret_b64* (the ENV_VAR_SECRET).

    Returns a URL-safe base64 authenticated blob suitable for
    the ``bottled_agent_secrets.value`` column."""
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


def is_legacy_blob(blob_b64: str) -> bool:
    """Whether *blob_b64* uses the pre-authentication storage format."""
    try:
        return not _b64dec(blob_b64).startswith(_VERSION)
    except (ValueError, TypeError):
        return False


def _decrypt_legacy(key: bytes, blob: bytes) -> str:
    """Read the original ``nonce || ciphertext`` format for migration only."""
    if len(blob) < _NONCE_BYTES:
        raise ValueError("ciphertext blob too short")
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    pt = bytearray()
    # The legacy format used the byte offset as the PRF counter.
    for i in range(0, len(ciphertext), _BLOCK):
        chunk = ciphertext[i : i + _BLOCK]
        ks = _keystream(key, nonce, i)[: len(chunk)]
        pt.extend(c ^ k for c, k in zip(chunk, ks))
    try:
        return bytes(pt).decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"decryption produced non-UTF-8 output: {exc}") from exc


def decrypt_value(secret_b64: str, blob_b64: str) -> str:
    """Decrypt a blob produced by :func:`encrypt_value`.

    Returns the original plaintext string. Raises ``ValueError`` for malformed
    input, authentication failure, or a key mismatch. Legacy unauthenticated
    rows remain readable so callers can migrate them immediately."""
    key = _b64dec(secret_b64)
    try:
        blob = _b64dec(blob_b64)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid ciphertext blob: {exc}") from exc
    if not blob.startswith(_VERSION):
        return _decrypt_legacy(key, blob)
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
    "is_legacy_blob",
]
