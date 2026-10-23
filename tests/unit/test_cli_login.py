"""Unit tests for bb login command."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from email.message import Message
from typing import Any
from unittest.mock import MagicMock, patch


class TestFlagParsing(unittest.TestCase):
    def test_console_url_flag(self) -> None:
        from bot_bottle.cli.commands.login import flag
        self.assertEqual(flag(["--console-url", "http://x"], "--console-url"), "http://x")

    def test_console_url_equals_form(self) -> None:
        from bot_bottle.cli.commands.login import flag
        self.assertEqual(
            flag(["--console-url=http://x"], "--console-url"), "http://x"
        )

    def test_label_flag(self) -> None:
        from bot_bottle.cli.commands.login import flag
        self.assertEqual(flag(["--label", "my-mac"], "--label"), "my-mac")

    def test_missing_flag_returns_none(self) -> None:
        from bot_bottle.cli.commands.login import flag
        self.assertIsNone(flag([], "--console-url"))


class TestHttpHelpers(unittest.TestCase):
    def test_post_sends_json_and_decodes_response(self) -> None:
        from bot_bottle.cli.commands.login import _post

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(
                _post("http://console/start", {"label": "host"}), {"ok": True}
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.data, b'{"label": "host"}')
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_get_decodes_success_response(self) -> None:
        from bot_bottle.cli.commands.login import _get

        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"status": "pending"}'
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(
                _get("http://console/status"), (200, {"status": "pending"})
            )

    def test_get_returns_http_error_status(self) -> None:
        from bot_bottle.cli.commands.login import _get

        error = urllib.error.HTTPError(
            "http://console/status", 410, "gone", Message(), None
        )
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(_get("http://console/status"), (410, {}))


class TestSaveCredentials(unittest.TestCase):
    def test_writes_json_and_sets_perms(self) -> None:
        from bot_bottle.cli.commands.login import _save_credentials

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

    def test_temp_file_is_private_before_replace(self) -> None:
        """Temp file must be 0600 at the moment os.replace is called."""
        from bot_bottle.cli.commands.login import _save_credentials
        from pathlib import Path as _Path

        tmp_perms_at_replace: list[int] = []
        real_replace = os.replace

        def _spy_replace(
            src: str | os.PathLike[str], dst: str | os.PathLike[str]
        ) -> None:
            tmp_perms_at_replace.append(_Path(src).stat().st_mode & 0o777)
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
                with patch("os.replace", side_effect=_spy_replace):
                    path = _save_credentials("http://c", "hid", "at", "rt")
                self.assertEqual(len(tmp_perms_at_replace), 1)
                self.assertEqual(oct(tmp_perms_at_replace[0]), oct(0o600))
                self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))

    def test_cleanup_on_write_failure(self) -> None:
        """Temp file is removed and no credentials remain if replace fails."""
        from bot_bottle.cli.commands.login import _save_credentials

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
                with patch("os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        _save_credentials("http://c", "hid", "at", "rt")
            leftovers = [f for f in os.listdir(tmp) if f.startswith(".console-")]
            self.assertEqual(leftovers, [])


class TestCmdLoginMissingUrl(unittest.TestCase):
    def test_help_returns_0(self) -> None:
        from bot_bottle.cli.commands.login import cmd_login

        self.assertEqual(cmd_login(["--help"]), 0)

    def test_returns_1_without_url(self) -> None:
        from bot_bottle.cli.commands.login import cmd_login
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BB_CONSOLE_URL", None)
            result = cmd_login([])
        self.assertEqual(result, 1)

    def test_reads_env_var(self) -> None:
        """Exits 1 (network error) not because of missing URL when env var is set."""
        from bot_bottle.cli.commands.login import cmd_login

        def _fail_post(_url: str, _payload: dict[str, Any]) -> dict[str, Any]:
            raise OSError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "BB_CONSOLE_URL": "http://localhost:9999",
                    "BOT_BOTTLE_ROOT": tmp,
                },
            ):
                with patch("bot_bottle.cli.commands.login._post", side_effect=_fail_post):
                    result = cmd_login([])
        self.assertEqual(result, 1)


class TestCmdLoginFlow(unittest.TestCase):
    def _run_with_mocks(
        self, poll_responses: list[dict[str, Any]], tmp: str
    ) -> int:
        from bot_bottle.cli.commands.login import cmd_login

        start_resp = {
            "device_code": "dc123",
            "user_code": "ABC-DEF",
            "expires_in": 300,
            "poll_interval": 0,
        }

        poll_iter = iter(poll_responses)

        def _fake_post(
            _url: str, _payload: dict[str, Any]
        ) -> dict[str, Any]:
            return start_resp

        def _fake_get(_url: str) -> tuple[int, dict[str, Any]]:
            return 200, next(poll_iter, {"status": "pending"})

        with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
            with patch("bot_bottle.cli.commands.login._post", side_effect=_fake_post):
                with patch("bot_bottle.cli.commands.login._get", side_effect=_fake_get):
                    with patch("time.sleep"):
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
            with open(os.path.join(tmp, "console.json"), encoding="utf-8") as f:
                creds = json.loads(f.read())
            self.assertEqual(creds["host_id"], "hid")

    def test_denied_flow_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_with_mocks([{"status": "denied"}], tmp)
        self.assertEqual(result, 1)

    def test_timeout_returns_1(self) -> None:
        from bot_bottle.cli.commands.login import cmd_login

        start_resp = {
            "device_code": "dc",
            "user_code": "ZZZ-ZZZ",
            "expires_in": 0,  # already expired; loop never runs
            "poll_interval": 2,
        }

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
                with patch("bot_bottle.cli.commands.login._post", return_value=start_resp):
                    result = cmd_login(["--console-url", "http://console"])
        self.assertEqual(result, 1)

    def test_poll_interval_from_server_is_used(self) -> None:
        """time.sleep must be called with the server-provided poll_interval."""
        from bot_bottle.cli.commands.login import cmd_login

        server_interval = 7
        start_resp = {
            "device_code": "dc",
            "user_code": "ABC-DEF",
            "expires_in": 300,
            "poll_interval": server_interval,
        }
        approved = {
            "status": "approved",
            "host_id": "hid",
            "access_token": "at",
            "refresh_token": "rt",
        }
        poll_iter = iter([{"status": "pending"}, approved])

        def _fake_get(_url: str) -> tuple[int, dict[str, str]]:
            return 200, next(poll_iter)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}):
                with patch("bot_bottle.cli.commands.login._post", return_value=start_resp):
                    with patch(
                        "bot_bottle.cli.commands.login._get", side_effect=_fake_get
                    ):
                        with patch("time.sleep") as mock_sleep:
                            result = cmd_login(["--console-url", "http://console"])

        self.assertEqual(result, 0)
        self.assertTrue(mock_sleep.called)
        for call in mock_sleep.call_args_list:
            self.assertEqual(call.args[0], server_interval)


class TestDispatcherRegistration(unittest.TestCase):
    def test_login_in_commands(self) -> None:
        from bot_bottle.cli import COMMANDS
        self.assertIn("login", COMMANDS)

    def test_login_in_no_migration(self) -> None:
        from bot_bottle.cli import NO_MIGRATION_COMMANDS
        self.assertIn("login", NO_MIGRATION_COMMANDS)


if __name__ == "__main__":
    unittest.main()
