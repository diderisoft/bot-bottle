"""Unit: `bot-bottle pair` — the QR code, the confirm code, then remote access."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from bot_bottle.cli.commands.pair import cmd_pair, render_qr
from bot_bottle.control.hostctl import remote
from bot_bottle.lib import qrcodegen

_START = {"device_code": "dc", "user_code": "ABC-DEF", "expires_in": 300, "poll_interval": 0,
          "confirm_code": "4F-7K", "pair_url": "https://console.example/pair?code=ABC-DEF"}
_APPROVED = {"status": "approved", "host_id": "hid", "access_token": "at",
             "refresh_token": "rt", "console_node_id": "cn", "console_ticket": "ct"}


class RenderQr(unittest.TestCase):
    def test_two_module_rows_per_line_inside_a_quiet_zone(self) -> None:
        url = str(_START["pair_url"])
        size = qrcodegen.QrCode.encode_text(url, qrcodegen.QrCode.Ecc.MEDIUM).get_size()
        lines = render_qr(url, ansi=False).split("\n")
        self.assertEqual((size + 4 + 1) // 2, len(lines))
        self.assertEqual({size + 4}, {len(line) for line in lines})
        self.assertLessEqual(set("".join(lines)), set(" ▀▄█"))
        self.assertEqual(" " * (size + 4), lines[0])  # the quiet zone

    def test_ansi_draws_dark_on_light(self) -> None:
        line = render_qr("x").split("\n")[1]
        self.assertTrue(line.startswith("\x1b[30;47m"))
        self.assertTrue(line.endswith("\x1b[0m"))


class CmdPair(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        env = patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BB_CONSOLE_URL", None)
        self.posted: list[dict[str, Any]] = []

    def run_pair(self, argv: list[str], polls: list[dict[str, Any]],
                 start: dict[str, Any] | None = None) -> tuple[int, str, list[list[str]]]:
        def fake_post(_url: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.posted.append(payload)
            return start or _START

        responses = iter(polls)
        enabled: list[list[str]] = []

        def fake_get(_url: str) -> tuple[int, dict[str, Any]]:
            return 200, next(responses, {"status": "pending"})

        def fake_remote(argv: list[str]) -> int:
            enabled.append(argv)
            return 0

        err = io.StringIO()
        with patch("bot_bottle.cli.commands.login._post", side_effect=fake_post), \
                patch("bot_bottle.cli.commands.login._get", side_effect=fake_get), \
                patch("time.sleep"), patch("sys.stderr", err), \
                patch.object(remote, "node_id", return_value="node"), \
                patch("bot_bottle.cli.commands.pair.cmd_remote", side_effect=fake_remote):
            status = cmd_pair(argv)
        return status, err.getvalue(), enabled

    def test_pairs_then_turns_on_remote_access(self) -> None:
        status, err, enabled = self.run_pair(["--console-url", "http://console/", "--label", "lap"],
                                             [{"status": "pending"}, _APPROVED])
        self.assertEqual(0, status)
        # It says which machine it is, so a re-pair stays with the host's owner.
        self.assertEqual([{"label": "lap", "confirm": True, "node_id": "node"}], self.posted)
        self.assertNotIn("already paired", err)
        self.assertIn("4F-7K", err)
        self.assertIn(_START["pair_url"], err)
        self.assertEqual([["enable"]], enabled)
        with open(os.path.join(self.root, "console.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(("http://console", "at", "cn"),
                         (saved["url"], saved["access_token"], saved["console_node_id"]))

    def test_an_already_paired_machine_is_told_to_sign_in(self) -> None:
        _, err, _ = self.run_pair(["--console-url", "http://c"], [_APPROVED],
                                  start={**_START, "owned": True})
        self.assertIn("already paired: sign in on your phone", err)

    def test_on_and_off_turn_the_link_on_and_off(self) -> None:
        with patch("bot_bottle.cli.commands.pair.cmd_remote", return_value=0) as rem:
            self.assertEqual(0, cmd_pair(["on"]))
            self.assertEqual(0, cmd_pair(["off"]))
        self.assertEqual([["enable"], ["disable"]], [c.args[0] for c in rem.call_args_list])

    def test_password_pairs_through_the_device_flow_then_turns_the_link_on(self) -> None:
        with patch("bot_bottle.cli.commands.login.cmd_login", return_value=0) as login, \
                patch("bot_bottle.cli.commands.pair.cmd_remote", return_value=0) as rem:
            self.assertEqual(0, cmd_pair(["--password", "--console-url", "http://c"]))
        login.assert_called_once_with(["--console-url", "http://c"])
        rem.assert_called_once_with(["enable"])
        with patch("bot_bottle.cli.commands.login.cmd_login", return_value=1), \
                patch("bot_bottle.cli.commands.pair.cmd_remote") as rem:
            self.assertEqual(1, cmd_pair(["--password"]))
        rem.assert_not_called()

    def test_a_denied_pairing_leaves_remote_access_off(self) -> None:
        status, _, enabled = self.run_pair(["--console-url", "http://c"], [{"status": "denied"}])
        self.assertEqual((1, []), (status, enabled))

    def test_a_console_without_confirm_codes_is_refused(self) -> None:
        start = {k: v for k, v in _START.items() if k not in ("confirm_code", "pair_url")}
        status, err, _ = self.run_pair(["--console-url", "http://c"], [], start)
        self.assertEqual(1, status)
        self.assertIn("bot-bottle pair --password", err)

    def test_without_iroh_it_stops_before_pairing(self) -> None:
        err = io.StringIO()
        with patch.object(remote, "node_id", side_effect=remote.RemoteError("needs iroh")), \
                patch("bot_bottle.cli.commands.login._post") as post, patch("sys.stderr", err):
            self.assertEqual(1, cmd_pair(["--console-url", "http://c"]))
        post.assert_not_called()
        self.assertIn("needs iroh", err.getvalue())

    def test_falls_back_to_the_console_last_logged_in_to(self) -> None:
        remote.save_console({"url": "http://remembered", "access_token": "x"})
        urls: list[str] = []

        def fake_post(url: str, _payload: dict[str, Any]) -> dict[str, Any]:
            urls.append(url)
            raise OSError("stop here")

        with patch("bot_bottle.cli.commands.login._post", side_effect=fake_post), \
                patch.object(remote, "node_id", return_value="node"), \
                patch("sys.stderr", io.StringIO()):
            cmd_pair([])
        self.assertEqual(["http://remembered/api/v1/hosts/authorize"], urls)

    def test_needs_a_console(self) -> None:
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(1, cmd_pair([]))

    def test_registered_and_store_free(self) -> None:
        from bot_bottle.cli.commands import COMMANDS, NO_MIGRATION_COMMANDS
        self.assertIn("pair", COMMANDS)
        self.assertIn("pair", NO_MIGRATION_COMMANDS)


if __name__ == "__main__":
    unittest.main()
