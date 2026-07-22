"""Unit tests for per-bottle egress secret encryption (PRD prd-new-secret-provider)."""

from __future__ import annotations

import unittest

from bot_bottle.orchestrator.secret_store import (
    ENV_VAR_SECRET_NAME,
    decrypt_value,
    encrypt_value,
    new_env_var_secret,
)


class TestNewEnvVarSecret(unittest.TestCase):
    def test_returns_non_empty_string(self) -> None:
        s = new_env_var_secret()
        self.assertIsInstance(s, str)
        self.assertTrue(len(s) > 0)

    def test_secrets_are_unique(self) -> None:
        keys = {new_env_var_secret() for _ in range(50)}
        self.assertEqual(50, len(keys))

    def test_no_padding_characters(self) -> None:
        # URL-safe base64, padding stripped — should round-trip cleanly
        for _ in range(20):
            self.assertNotIn("=", new_env_var_secret())


class TestEncryptDecryptRoundtrip(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = new_env_var_secret()

    def _rt(self, plaintext: str) -> str:
        return decrypt_value(self.secret, encrypt_value(self.secret, plaintext))

    def test_roundtrip_short_value(self) -> None:
        self.assertEqual("sk-abc123", self._rt("sk-abc123"))

    def test_roundtrip_empty_string(self) -> None:
        self.assertEqual("", self._rt(""))

    def test_roundtrip_long_value_crosses_block_boundary(self) -> None:
        # 32 bytes is exactly one HMAC-SHA256 block; 65 bytes crosses two.
        plaintext = "x" * 65
        self.assertEqual(plaintext, self._rt(plaintext))

    def test_roundtrip_unicode(self) -> None:
        self.assertEqual("héllo wörld", self._rt("héllo wörld"))

    def test_encrypt_produces_different_ciphertexts_each_call(self) -> None:
        ct1 = encrypt_value(self.secret, "same")
        ct2 = encrypt_value(self.secret, "same")
        self.assertNotEqual(ct1, ct2)  # fresh nonce each call

    def test_ciphertext_is_url_safe_base64(self) -> None:
        ct = encrypt_value(self.secret, "hello")
        # no '+', '/', '=' — URL-safe and padding-stripped
        for ch in ("+", "/", "="):
            self.assertNotIn(ch, ct)


class TestDecryptErrors(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = new_env_var_secret()

    def test_wrong_key_raises_value_error(self) -> None:
        ct = encrypt_value(self.secret, "secret-token")
        other_key = new_env_var_secret()
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            decrypt_value(other_key, ct)

    def test_modified_ciphertext_raises_value_error(self) -> None:
        ct = encrypt_value(self.secret, "secret-token")
        replacement = "A" if ct[-10] != "A" else "B"
        tampered = ct[:-10] + replacement + ct[-9:]
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            decrypt_value(self.secret, tampered)

    def test_modified_tag_raises_value_error(self) -> None:
        ct = encrypt_value(self.secret, "secret-token")
        replacement = "A" if ct[-1] != "A" else "B"
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            decrypt_value(self.secret, ct[:-1] + replacement)

    def test_truncated_blob_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            decrypt_value(self.secret, "dG9vc2hvcnQ")  # "tooshort" — under 16 nonce bytes

    def test_invalid_base64_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            decrypt_value(self.secret, "!!not-base64!!")


class TestConstant(unittest.TestCase):
    def test_env_var_secret_name(self) -> None:
        self.assertEqual("ENV_VAR_SECRET", ENV_VAR_SECRET_NAME)


if __name__ == "__main__":
    unittest.main()
