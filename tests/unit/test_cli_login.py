"""Unit tests for bb login command."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestFlagParsing(unittest.TestCase):
    def test_console_url_flag(self) -> None:
        from bot_bottle.cli.login import _flag
        self.assertEqual(_flag(["--console-url", "http://x"], "--console-url"), "http://x")

    def test_console_url_equals_form(self) -> None:
        from bot_bottle.cli.login import _flag
        self.assertEqual(_flag(["--console-url=http://x"], "--console-url"), "http://x")

    def test_label_flag(self) -> None:
        from bot_bottle.cli.login import _flag
        self.assertEqual(_flag(["--label", "my-mac"], "--label"), "my-mac")

    def test_missing_flag_returns_none(self) -> None:
        from bot_bottle.cli.login import _flag
        self.assertIsNone(_flag([], "--console-url"))


class TestSaveCredentials(unittest.TestCase):
    def test_writes_json_and_sets_perms(self) -> None:
        from bot_bottle.cli.login import _save_credentials

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
                path = _save_credentials("http://c", "hid", "at", "rt")
            self.assertTrue(path.exists())
            data = json.loads(path.read_text())
            self.assertEqual(data["url"], "http://c")
            self.assertEqual(data["host_id"], "hid")
            self.assertEqual(data["access_token"], "at")
            self.assertEqual(data["refresh_token"], "rt")
            self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))


class TestCmdLoginMissingUrl(unittest.TestCase):
    def test_returns_1_without_url(self) -> None:
        from bot_bottle.cli.login import cmd_login
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BB_CONSOLE_URL", None)
            result = cmd_login([])
        self.assertEqual(result, 1)

    def test_reads_env_var(self) -> None:
        """Exits 1 (network error) not because of missing URL when env var is set."""
        from bot_bottle.cli.login import cmd_login

        def _fail_post(url, payload):
            raise OSError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BB_CONSOLE_URL": "http://localhost:9999", "BOT_BOTTLE_ROOT": tmp}):
                with patch("bot_bottle.cli.login._post", side_effect=_fail_post):
                    result = cmd_login([])
        self.assertEqual(result, 1)


class TestCmdLoginFlow(unittest.TestCase):
    def _run_with_mocks(self, poll_responses: list[dict], tmp: str) -> int:
        from bot_bottle.cli.login import cmd_login

        start_resp = {
            "device_code": "dc123",
            "user_code": "ABC-DEF",
            "expires_in": 300,
            "poll_interval": 0,
        }

        poll_iter = iter(poll_responses)

        def _fake_post(url, payload):
            return start_resp

        def _fake_get(url):
            try:
                resp = next(poll_iter)
            except StopIteration:
                return 200, {"status": "pending"}
            return 200, resp

        with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
            with patch("bot_bottle.cli.login._post", side_effect=_fake_post):
                with patch("bot_bottle.cli.login._get", side_effect=_fake_get):
                    with patch("bot_bottle.cli.login._POLL_SLEEP", 0):
                        return cmd_login(["--console-url", "http://console"])

    def test_approved_flow_returns_0(self) -> None:
        approved = {
            "status": "approved",
            "host_id": "hid",
            "access_token": "at",
            "refresh_token": "rt",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_with_mocks(
                [{"status": "pending"}, approved], tmp
            )
            self.assertEqual(result, 0)
            with open(os.path.join(tmp, "console.json")) as f:
                creds = json.loads(f.read())
            self.assertEqual(creds["host_id"], "hid")

    def test_denied_flow_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_with_mocks([{"status": "denied"}], tmp)
        self.assertEqual(result, 1)

    def test_timeout_returns_1(self) -> None:
        from bot_bottle.cli.login import cmd_login

        start_resp = {
            "device_code": "dc",
            "user_code": "ZZZ-ZZZ",
            "expires_in": 0,  # already expired
            "poll_interval": 0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
                with patch("bot_bottle.cli.login._post", return_value=start_resp):
                    with patch("bot_bottle.cli.login._POLL_SLEEP", 0):
                        result = cmd_login(["--console-url", "http://console"])
        self.assertEqual(result, 1)


class TestDispatcherRegistration(unittest.TestCase):
    def test_login_in_commands(self) -> None:
        from bot_bottle.cli import COMMANDS
        self.assertIn("login", COMMANDS)

    def test_login_in_no_migration(self) -> None:
        from bot_bottle.cli import NO_MIGRATION_COMMANDS
        self.assertIn("login", NO_MIGRATION_COMMANDS)


if __name__ == "__main__":
    unittest.main()
