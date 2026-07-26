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

Encryption scheme: HMAC-SHA256 used as a PRF in CTR mode (stdlib-only,
no external deps). Each value is encrypted independently. The output blob is
``nonce (16 bytes) || ciphertext`` encoded as URL-safe base64 (no padding).

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

# Env-var name the agent container receives at startup.
ENV_VAR_SECRET_NAME = "ENV_VAR_SECRET"


def new_env_var_secret() -> str:
    """Generate a fresh ENV_VAR_SECRET: 32 random bytes as URL-safe base64."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).rstrip(b"=").decode()


def _b64dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _keystream(key: bytes, nonce: bytes, block_index: int) -> bytes:
    return hmac.new(
        key, nonce + block_index.to_bytes(4, "big"), hashlib.sha256
    ).digest()


def encrypt_value(secret_b64: str, plaintext: str) -> str:
    """Encrypt a single string value with *secret_b64* (the ENV_VAR_SECRET).

    Returns a URL-safe base64 blob ``nonce || ciphertext`` suitable for
    the ``bottled_agent_secrets.value`` column."""
    key = _b64dec(secret_b64)
    pt = plaintext.encode()
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ct = bytearray()
    for i in range(0, len(pt), _BLOCK):
        chunk = pt[i : i + _BLOCK]
        ks = _keystream(key, nonce, i)[: len(chunk)]
        ct.extend(p ^ k for p, k in zip(chunk, ks))
    return base64.urlsafe_b64encode(nonce + bytes(ct)).rstrip(b"=").decode()


def decrypt_value(secret_b64: str, blob_b64: str) -> str:
    """Decrypt a blob produced by :func:`encrypt_value`.

    Returns the original plaintext string. Raises ``ValueError`` for malformed
    input or a key mismatch (wrong key produces garbage, not an error, unless
    the plaintext is non-UTF-8 — treat all such failures as wrong key)."""
    key = _b64dec(secret_b64)
    try:
        blob = _b64dec(blob_b64)
    except Exception as exc:
        raise ValueError(f"invalid ciphertext blob: {exc}") from exc
    if len(blob) < _NONCE_BYTES:
        raise ValueError("ciphertext blob too short")
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    pt = bytearray()
    for i in range(0, len(ciphertext), _BLOCK):
        chunk = ciphertext[i : i + _BLOCK]
        ks = _keystream(key, nonce, i)[: len(chunk)]
        pt.extend(c ^ k for c, k in zip(chunk, ks))
    try:
        return bytes(pt).decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"decryption produced non-UTF-8 output (wrong key?): {exc}") from exc


__all__ = ["ENV_VAR_SECRET_NAME", "new_env_var_secret", "encrypt_value", "decrypt_value"]
