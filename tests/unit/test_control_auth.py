"""Unit: role-scoped control-plane tokens (issue #469 review)."""

from __future__ import annotations

import base64
import json
import unittest

from bot_bottle.control_auth import ROLE_CLI, ROLE_GATEWAY, ROLES, mint, verify

_KEY = "test-key"


def _b64(obj: object) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode()).rstrip(b"=").decode("ascii")


class TestMintVerify(unittest.TestCase):
    def test_round_trip_each_role(self) -> None:
        for role in ROLES:
            self.assertEqual(role, verify(mint(role, _KEY), _KEY))

    def test_gateway_and_cli_are_distinct(self) -> None:
        self.assertEqual(ROLE_GATEWAY, verify(mint(ROLE_GATEWAY, _KEY), _KEY))
        self.assertEqual(ROLE_CLI, verify(mint(ROLE_CLI, _KEY), _KEY))

    def test_wrong_key_rejected(self) -> None:
        self.assertIsNone(verify(mint(ROLE_GATEWAY, _KEY), "other-key"))

    def test_tampered_signature_rejected(self) -> None:
        tok = mint(ROLE_CLI, _KEY)
        self.assertIsNone(verify(tok[:-1] + ("A" if tok[-1] != "A" else "B"), _KEY))

    def test_tampered_payload_rejected(self) -> None:
        header, _payload, sig = mint(ROLE_GATEWAY, _KEY).split(".")
        forged = f"{header}.{_b64({'role': 'cli'})}.{sig}"
        self.assertIsNone(verify(forged, _KEY))

    def test_alg_none_rejected(self) -> None:
        # An unsigned "alg: none" token must never verify.
        tok = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'role': 'cli'})}."
        self.assertIsNone(verify(tok, _KEY))

    def test_validly_signed_but_wrong_alg_rejected(self) -> None:
        # Alg-confusion: even a *correctly signed* token whose header claims a
        # non-HS256 alg must be rejected.
        from bot_bottle.control_auth import _sign  # noqa: PLC0415
        signing_input = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'role': 'cli'})}"
        self.assertIsNone(verify(f"{signing_input}.{_sign(_KEY, signing_input)}", _KEY))

    def test_validly_signed_but_undecodable_rejected(self) -> None:
        # A correct signature over a header that isn't valid base64/JSON still
        # fails closed rather than raising.
        from bot_bottle.control_auth import _sign  # noqa: PLC0415
        signing_input = f"!!!not-base64!!!.{_b64({'role': 'cli'})}"
        self.assertIsNone(verify(f"{signing_input}.{_sign(_KEY, signing_input)}", _KEY))

    def test_malformed_tokens_rejected(self) -> None:
        for bad in ("", "a", "a.b", "a.b.c.d", "not-base64.$$.$$"):
            self.assertIsNone(verify(bad, _KEY))

    def test_empty_key_never_verifies(self) -> None:
        self.assertIsNone(verify(mint(ROLE_CLI, _KEY), ""))

    def test_unknown_role_in_payload_rejected(self) -> None:
        header, _p, _s = mint(ROLE_CLI, _KEY).split(".")
        # Re-sign a token carrying an unknown role — a valid signature but a
        # role the control plane doesn't recognise must still be rejected.
        from bot_bottle.control_auth import _sign  # noqa: PLC0415
        payload = _b64({"role": "root"})
        signing_input = f"{header}.{payload}"
        forged = f"{signing_input}.{_sign(_KEY, signing_input)}"
        self.assertIsNone(verify(forged, _KEY))

    def test_mint_rejects_unknown_role(self) -> None:
        with self.assertRaises(ValueError):
            mint("root", _KEY)

    def test_mint_rejects_empty_key(self) -> None:
        with self.assertRaises(ValueError):
            mint(ROLE_GATEWAY, "")


if __name__ == "__main__":
    unittest.main()
