"""Unit: host Codex auth extraction."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bot_bottle.codex_auth import (
    codex_auth_path,
    codex_dummy_auth_json,
    codex_host_access_token,
)
from bot_bottle.log import Die


def _jwt(exp: int) -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


def _jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())


class TestCodexHostAccessToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cb-codex-auth.")
        self.home = Path(self.tmp.name)
        self.auth_path = self.home / "auth.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload: dict) -> None:
        self.auth_path.write_text(json.dumps(payload))

    def test_auth_path_uses_codex_home(self):
        self.assertEqual(
            self.auth_path,
            codex_auth_path({"CODEX_HOME": str(self.home)}),
        )

    def test_returns_fresh_chatgpt_access_token(self):
        token = _jwt(2000000000)
        self._write({
            "auth_mode": "chatgpt",
            "tokens": {"access_token": token, "refresh_token": "hidden"},
        })
        out = codex_host_access_token(
            {"CODEX_HOME": str(self.home)},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(token, out)

    def test_missing_auth_file_dies(self):
        with self.assertRaises(Die):
            codex_host_access_token({"CODEX_HOME": str(self.home)})

    def test_non_chatgpt_auth_dies(self):
        self._write({"auth_mode": "api_key", "tokens": {"access_token": _jwt(2)}})
        with self.assertRaises(Die):
            codex_host_access_token({"CODEX_HOME": str(self.home)})

    def test_user_auth_mode_is_allowed(self):
        token = _jwt(2000000000)
        self._write({"auth_mode": "user", "tokens": {"access_token": token}})
        out = codex_host_access_token(
            {"CODEX_HOME": str(self.home)},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(token, out)

    def test_expired_token_dies(self):
        self._write({
            "auth_mode": "chatgpt",
            "tokens": {"access_token": _jwt(1000)},
        })
        with self.assertRaises(Die):
            codex_host_access_token(
                {"CODEX_HOME": str(self.home)},
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    def test_non_jwt_token_dies(self):
        self._write({
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "not-a-jwt"},
        })
        with self.assertRaises(Die):
            codex_host_access_token({"CODEX_HOME": str(self.home)})

    def test_dummy_auth_preserves_mode_and_redacts_tokens(self):
        access = _jwt(2000000000)
        refresh = "host-refresh-token"
        self._write({
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": access,
                "id_token": _jwt(2000000000),
                "refresh_token": refresh,
                "account_id": "acct-host",
            },
            "last_refresh": "2026-05-29T00:00:00.000Z",
        })
        dummy = json.loads(codex_dummy_auth_json(
            {"CODEX_HOME": str(self.home)},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        self.assertEqual("chatgpt", dummy["auth_mode"])
        self.assertIsNone(dummy["OPENAI_API_KEY"])
        self.assertNotEqual(access, dummy["tokens"]["access_token"])
        self.assertNotEqual(refresh, dummy["tokens"]["refresh_token"])
        self.assertEqual("bot-bottle-placeholder", dummy["tokens"]["refresh_token"])
        self.assertEqual("bot-bottle-placeholder", dummy["tokens"]["account_id"])
        self.assertIsNotNone(
            codex_host_access_token(
                {"CODEX_HOME": str(self.home)},
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    def test_dummy_auth_keeps_required_account_claim_shape(self):
        def jwt(payload: dict) -> str:
            def enc(obj: dict) -> str:
                raw = json.dumps(obj, separators=(",", ":")).encode()
                return base64.urlsafe_b64encode(raw).decode().rstrip("=")
            return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"

        self._write({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": jwt({
                    "exp": 2000000000,
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "plus",
                        "chatgpt_account_id": "acct-real",
                        "chatgpt_user_id": "user-real",
                        "user_id": "auth-user-real",
                        "localhost": True,
                    },
                    "https://api.openai.com/profile": {
                        "email": "real@example.invalid",
                        "email_verified": True,
                    },
                }),
                "id_token": jwt({
                    "exp": 2000000000,
                    "email": "real@example.invalid",
                    "email_verified": True,
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "plus",
                        "chatgpt_account_id": "acct-real",
                    },
                }),
                "refresh_token": "hidden",
            },
        })
        dummy = json.loads(codex_dummy_auth_json(
            {"CODEX_HOME": str(self.home)},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        access_payload = _jwt_payload(dummy["tokens"]["access_token"])
        auth = access_payload["https://api.openai.com/auth"]
        profile = access_payload["https://api.openai.com/profile"]
        self.assertEqual("plus", auth["chatgpt_plan_type"])
        self.assertEqual("bot-bottle-placeholder", auth["chatgpt_account_id"])
        self.assertEqual("bot-bottle-placeholder", auth["chatgpt_user_id"])
        self.assertEqual("bot-bottle@example.invalid", profile["email"])
        self.assertTrue(profile["email_verified"])


if __name__ == "__main__":
    unittest.main()
