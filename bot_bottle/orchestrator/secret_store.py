"""Symmetric encryption for per-bottle egress secrets (PRD prd-new-secret-provider).

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

Encryption scheme: HMAC-SHA256 used as a PRF in CTR mode plus an independent
HMAC-SHA256 authentication key (stdlib-only, no external deps). Each value is
encrypted independently. The output blob is ``nonce (16 bytes) || ciphertext
|| tag (32 bytes)`` encoded as URL-safe base64 (no padding). Both subkeys are
derived from ENV_VAR_SECRET with domain-separated HMAC labels.

    keystream_block_i = HMAC-SHA256(key, nonce || i.to_bytes(4, "big"))
    ciphertext_i      = plaintext_i XOR keystream_block_i[:len(plaintext_i)]
    tag               = HMAC-SHA256(auth_key, nonce || ciphertext)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_KEY_BYTES = 32    # 256-bit key from ENV_VAR_SECRET
_NONCE_BYTES = 16  # 128-bit random nonce per encrypt call
_BLOCK = 32        # HMAC-SHA256 output width == one keystream block
_TAG_BYTES = 32    # full HMAC-SHA256 authentication tag

# Env-var name the agent container receives at startup.
ENV_VAR_SECRET_NAME = "ENV_VAR_SECRET"


def new_env_var_secret() -> str:
    """Generate a fresh ENV_VAR_SECRET: 32 random bytes as URL-safe base64."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).rstrip(b"=").decode()


def _b64dec(s: str) -> bytes:
    return base64.b64decode(
        s + "=" * (-len(s) % 4), altchars=b"-_", validate=True,
    )


def _keystream(key: bytes, nonce: bytes, block_index: int) -> bytes:
    return hmac.new(
        key, nonce + block_index.to_bytes(4, "big"), hashlib.sha256
    ).digest()


def _subkeys(key: bytes) -> tuple[bytes, bytes]:
    """Derive independent encryption and authentication keys."""
    return (
        hmac.new(key, b"bot-bottle secret encryption v1", hashlib.sha256).digest(),
        hmac.new(key, b"bot-bottle secret authentication v1", hashlib.sha256).digest(),
    )


def encrypt_value(secret_b64: str, plaintext: str) -> str:
    """Encrypt a single string value with *secret_b64* (the ENV_VAR_SECRET).

    Returns a URL-safe base64 blob ``nonce || ciphertext || tag`` suitable for
    the ``bottled_agent_secrets.value`` column."""
    enc_key, auth_key = _subkeys(_b64dec(secret_b64))
    pt = plaintext.encode()
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ct = bytearray()
    for i in range(0, len(pt), _BLOCK):
        chunk = pt[i : i + _BLOCK]
        ks = _keystream(enc_key, nonce, i)[: len(chunk)]
        ct.extend(p ^ k for p, k in zip(chunk, ks))
    authenticated = nonce + bytes(ct)
    tag = hmac.new(auth_key, authenticated, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(authenticated + tag).rstrip(b"=").decode()


def decrypt_value(secret_b64: str, blob_b64: str) -> str:
    """Decrypt a blob produced by :func:`encrypt_value`.

    Returns the original plaintext string. Raises ``ValueError`` for malformed
    input, tampering, or a key mismatch. Authentication is verified before any
    plaintext is returned."""
    enc_key, auth_key = _subkeys(_b64dec(secret_b64))
    try:
        blob = _b64dec(blob_b64)
    except Exception as exc:
        raise ValueError(f"invalid ciphertext blob: {exc}") from exc
    if len(blob) < _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("ciphertext blob too short")
    authenticated, tag = blob[:-_TAG_BYTES], blob[-_TAG_BYTES:]
    expected = hmac.new(auth_key, authenticated, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("ciphertext authentication failed")
    nonce, ciphertext = authenticated[:_NONCE_BYTES], authenticated[_NONCE_BYTES:]
    pt = bytearray()
    for i in range(0, len(ciphertext), _BLOCK):
        chunk = ciphertext[i : i + _BLOCK]
        ks = _keystream(enc_key, nonce, i)[: len(chunk)]
        pt.extend(c ^ k for c, k in zip(chunk, ks))
    try:
        return bytes(pt).decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"decryption produced non-UTF-8 output (wrong key?): {exc}") from exc


__all__ = ["ENV_VAR_SECRET_NAME", "new_env_var_secret", "encrypt_value", "decrypt_value"]
