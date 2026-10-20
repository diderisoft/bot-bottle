"""Unit: host Claude auth extraction."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.contrib.claude.claude_auth import (
    claude_auth_path,
    claude_host_access_token,
)
from bot_bottle.lib.log import Die


def _cred_json(access_token: str, **extra: object) -> str:
    payload: dict[str, object] = {"claudeAiOauth": {"accessToken": access_token, **extra}}
    return json.dumps(payload)


class TestClaudeHostAccessToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bb-claude-auth.")
        self.home = Path(self.tmp.name)
        self.cred_dir = self.home / ".claude"
        self.cred_dir.mkdir()
        self.auth_path = self.cred_dir / ".credentials.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload: dict) -> None:  # type: ignore[no-untyped-def]
        self.auth_path.write_text(json.dumps(payload))

    def test_auth_path_uses_home_env(self):
        self.assertEqual(
            self.auth_path,
            claude_auth_path({"HOME": str(self.home)}),
        )

    # --- file-based (Linux) ---

    def test_file_returns_access_token(self):
        key = "sk-ant-oat01-real-key"  # gitleaks:allow
        self._write({"claudeAiOauth": {"accessToken": key}})
        out = claude_host_access_token({"HOME": str(self.home)})
        self.assertEqual(key, out)

    def test_file_missing_claude_ai_oauth_dies(self):
        self._write({"hasCompletedOnboarding": True})
        with self.assertRaises(Die):
            claude_host_access_token({"HOME": str(self.home)})

    def test_file_missing_access_token_dies(self):
        self._write({"claudeAiOauth": {"expiresAt": 2000000000000}})
        with self.assertRaises(Die):
            claude_host_access_token({"HOME": str(self.home)})

    def test_file_empty_access_token_dies(self):
        self._write({"claudeAiOauth": {"accessToken": ""}})
        with self.assertRaises(Die):
            claude_host_access_token({"HOME": str(self.home)})

    def test_file_expired_token_dies(self):
        # expiresAt is milliseconds; 1_000_000 ms is year 1970
        self._write({
            "claudeAiOauth": {"accessToken": "sk-ant-oat01-x", "expiresAt": 1_000_000},  # gitleaks:allow
        })
        with self.assertRaises(Die):
            claude_host_access_token(
                {"HOME": str(self.home)},
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    def test_file_future_expiry_is_accepted(self):
        key = "sk-ant-oat01-y"  # gitleaks:allow
        # 2_000_000_000_000 ms ≈ year 2033
        self._write({
            "claudeAiOauth": {"accessToken": key, "expiresAt": 2_000_000_000_000},
        })
        out = claude_host_access_token(
            {"HOME": str(self.home)},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(key, out)

    def test_file_absent_expiry_is_accepted(self):
        key = "sk-ant-oat01-z"  # gitleaks:allow
        self._write({"claudeAiOauth": {"accessToken": key}})
        out = claude_host_access_token({"HOME": str(self.home)})
        self.assertEqual(key, out)

    def test_file_non_json_dies(self):
        self.auth_path.write_text("not json {{{")
        with self.assertRaises(Die):
            claude_host_access_token({"HOME": str(self.home)})

    def test_file_json_array_root_dies(self):
        self.auth_path.write_text("[]")
        with self.assertRaises(Die):
            claude_host_access_token({"HOME": str(self.home)})

    def test_file_extra_fields_are_ignored(self):
        key = "sk-ant-oat01-real"  # gitleaks:allow
        self._write({
            "claudeAiOauth": {
                "accessToken": key,
                "refreshToken": "sk-ant-ort01-secret",  # gitleaks:allow
                "scopes": ["user:inference"],
                "expiresAt": 2_000_000_000_000,
            },
        })
        out = claude_host_access_token({"HOME": str(self.home)})
        self.assertEqual(key, out)

    # --- macOS Keychain fallback ---

    def _home_without_creds(self) -> Path:
        """A home dir that has .claude/ but no .credentials.json."""
        empty = self.home / "no-creds"
        (empty / ".claude").mkdir(parents=True)
        return empty

    def _mock_keychain(self, stdout: str, returncode: int = 0) -> MagicMock:
        mock = MagicMock()
        mock.returncode = returncode
        mock.stdout = stdout
        return mock

    def test_keychain_used_when_file_absent(self):
        key = "sk-ant-oat01-keychain"  # gitleaks:allow
        home = self._home_without_creds()
        with patch(
            "bot_bottle.contrib.claude.claude_auth.subprocess.run",
            return_value=self._mock_keychain(_cred_json(key)),
        ), patch(
            "bot_bottle.contrib.claude.claude_auth.sys.platform", "darwin",
        ):
            out = claude_host_access_token({"HOME": str(home)})
        self.assertEqual(key, out)

    def test_keychain_failure_when_file_absent_dies(self):
        home = self._home_without_creds()
        with patch(
            "bot_bottle.contrib.claude.claude_auth.subprocess.run",
            return_value=self._mock_keychain("", returncode=44),
        ), patch(
            "bot_bottle.contrib.claude.claude_auth.sys.platform", "darwin",
        ):
            with self.assertRaises(Die):
                claude_host_access_token({"HOME": str(home)})

    def test_no_file_no_keychain_on_linux_dies(self):
        home = self._home_without_creds()
        with patch("bot_bottle.contrib.claude.claude_auth.sys.platform", "linux"):
            with self.assertRaises(Die):
                claude_host_access_token({"HOME": str(home)})

    def test_keychain_non_json_dies(self):
        home = self._home_without_creds()
        with patch(
            "bot_bottle.contrib.claude.claude_auth.subprocess.run",
            return_value=self._mock_keychain("not-json"),
        ), patch(
            "bot_bottle.contrib.claude.claude_auth.sys.platform", "darwin",
        ):
            with self.assertRaises(Die):
                claude_host_access_token({"HOME": str(home)})

    def test_keychain_security_not_found_dies(self):
        home = self._home_without_creds()
        with patch(
            "bot_bottle.contrib.claude.claude_auth.subprocess.run",
            side_effect=FileNotFoundError,
        ), patch(
            "bot_bottle.contrib.claude.claude_auth.sys.platform", "darwin",
        ):
            with self.assertRaises(Die):
                claude_host_access_token({"HOME": str(home)})


if __name__ == "__main__":
    unittest.main()
