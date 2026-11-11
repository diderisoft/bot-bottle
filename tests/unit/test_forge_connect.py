"""`bot-bottle forge connect` (PRD 0103)."""

from __future__ import annotations

import contextlib
import email.message
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands.forge import cmd_forge
from bot_bottle.contrib.gitea import forge_api
from bot_bottle.forge_connect import ConnectError, connected_secret, ssh_host_port
from bot_bottle.forge_connections import ForgeConnection, ForgeStore, ForgeUser
from bot_bottle.lib.log import Die
from bot_bottle.paths import host_db_path
from tests.unit import as_cli, serve_host_controller, use_bottle_root


class TestConnectedSecret(unittest.TestCase):
    def test_a_name_another_connection_holds_is_refused(self) -> None:
        held = {"a-b.example": ForgeConnection(
            host="a-b.example", kind="gitea", url="https://a-b.example",
            ssh="ssh://git@a-b.example",
            users=(ForgeUser("bot", "BB_HOST_FORGE_A_B_EXAMPLE__BOT_TOKEN"),))}
        with mock.patch("bot_bottle.forge_connect.connections", return_value=held):
            self.assertEqual("BB_HOST_FORGE_A_B_EXAMPLE__BOT_TOKEN",
                             connected_secret("a-b.example", "bot"))
            with self.assertRaisesRegex(ConnectError, "disconnect it first"):
                connected_secret("a.b.example", "bot")
            self.assertEqual("BB_HOST_FORGE_A_B_EXAMPLE__CODEX_TOKEN",
                             connected_secret("a.b.example", "codex"))


class TestSshHostPort(unittest.TestCase):
    def test_ssh_host_port(self) -> None:
        self.assertEqual(("10.0.0.1", "30009"), ssh_host_port("ssh://git@10.0.0.1:30009"))
        self.assertEqual(("h", "22"), ssh_host_port("ssh://git@h"))


def _response(body: object) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    return resp


class TestForgeApi(unittest.TestCase):
    def test_ssh_base(self) -> None:
        self.assertEqual("ssh://git@h:2222", forge_api.ssh_base("ssh://git@h:2222/o/r.git"))
        self.assertEqual("ssh://git@h", forge_api.ssh_base("git@h:o/r.git"))

    def test_check_token(self) -> None:
        replies = [_response({"login": "bot"}), _response([
            {"ssh_url": "ssh://git@h:2222/o/a.git", "permissions": {"admin": True}},
            {"ssh_url": "ssh://git@h:2222/o/b.git", "permissions": {"admin": False}},
        ])]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as urlopen:
            check = forge_api.check_token("https://h/", "tok")
        self.assertEqual(forge_api.TokenCheck("bot", "ssh://git@h:2222", 1, 2), check)
        req = urlopen.call_args_list[0].args[0]
        self.assertEqual("https://h/api/v1/user", req.full_url)
        self.assertEqual("token tok", req.get_header("Authorization"))

    def test_errors(self) -> None:
        err = urllib.error.HTTPError("u", 403, "no", email.message.Message(),
                                     io.BytesIO(b"missing scope read:user"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(forge_api.ForgeApiError, "403.*read:user"):
                forge_api.check_token("https://h", "tok")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            self.assertFalse(forge_api.is_gitea("https://h"))
        with mock.patch("urllib.request.urlopen", return_value=_response({"version": "1"})):
            self.assertTrue(forge_api.is_gitea("https://h"))
        with mock.patch("urllib.request.urlopen", return_value=_response({})):
            with self.assertRaises(forge_api.ForgeApiError):
                forge_api.check_token("https://h", "tok")


class TestCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.root = self.tmp / "root"
        self.addCleanup(use_bottle_root(self.root))
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.enterContext(mock.patch.object(forge_api, "is_gitea", return_value=True))
        self.check = self.enterContext(mock.patch.object(
            forge_api, "check_token",
            return_value=forge_api.TokenCheck("bot", "ssh://git@10.0.0.1:30009", 1, 1)))
        self.enterContext(mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                                     return_value="ssh-ed25519 AAAA"))

    def run_cmd(self, *argv: str, token: str = "s3cret-token\n") -> None:
        with mock.patch("sys.stdin", io.StringIO(token)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cmd_forge(["connect", *argv, "--token-stdin"]))

    def test_records_the_connection_and_the_secret(self) -> None:
        self.run_cmd("gitea.example.com")
        self.assertNotIn(b"s3cret", host_db_path().read_bytes())
        self.assertFalse((self.root / "bottles" / "gitea.md").exists())
        self.assertFalse((self.root / "forges.md").exists())
        conn = ForgeStore(host_db_path()).connections()["gitea.example.com"]
        self.assertEqual(("gitea", "https://gitea.example.com", "ssh://git@10.0.0.1:30009",
                          "ssh-ed25519 AAAA"), (conn.kind, conn.url, conn.ssh, conn.host_key))
        self.assertEqual([("bot", "BB_HOST_FORGE_GITEA_EXAMPLE_COM__BOT_TOKEN", True)],
                         [(u.name, u.token_secret, u.default) for u in conn.users])
        secrets = (self.root / "secrets").read_text()
        self.assertIn("BB_HOST_FORGE_GITEA_EXAMPLE_COM__BOT_TOKEN=s3cret-token", secrets)
        self.check.assert_called_once_with("https://gitea.example.com", "s3cret-token")

    def test_another_user_and_the_default(self) -> None:
        self.run_cmd("gitea.example.com")
        self.check.return_value = forge_api.TokenCheck("codex", "", 1, 1)
        self.run_cmd("gitea.example.com", token="other\n")
        users = ForgeStore(host_db_path()).connections()["gitea.example.com"].users
        self.assertEqual([("bot", "BB_HOST_FORGE_GITEA_EXAMPLE_COM__BOT_TOKEN", True),
                          ("codex", "BB_HOST_FORGE_GITEA_EXAMPLE_COM__CODEX_TOKEN", False)],
                         [(u.name, u.token_secret, u.default) for u in users])
        self.run_cmd("gitea.example.com", "--default", token="other\n")
        users = ForgeStore(host_db_path()).connections()["gitea.example.com"].users
        self.assertEqual({"codex"}, {u.name for u in users if u.default})
        self.assertEqual("BB_HOST_FORGE_GITEA_EXAMPLE_COM__CODEX_TOKEN", users[-1].token_secret)

    def test_ssh_override(self) -> None:
        self.run_cmd("gitea.example.com", "--ssh", "ssh://git@100.64.0.1:30009")
        conn = ForgeStore(host_db_path()).connections()["gitea.example.com"]
        self.assertEqual("ssh://git@100.64.0.1:30009", conn.ssh)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Die):
            self.run_cmd("gitea.example.com", "--ssh", "100.64.0.1")

    def test_transport_is_githubs(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Die):
            self.run_cmd("gitea.example.com", "--transport", "deploy-key")

    def test_refusals(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, cmd_forge([]))
            with self.assertRaises(Die):
                cmd_forge(["connect"])
            with self.assertRaises(Die):
                self.run_cmd("gitea.example.com", token="\n")
            with mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                            side_effect=RuntimeError("no host key")), \
                    self.assertRaises(Die):
                self.run_cmd("gitea.example.com")
            self.assertFalse((self.root / "secrets").exists())  # nothing stored
            self.check.side_effect = forge_api.ForgeApiError("HTTP 401")
            with self.assertRaises(Die):
                self.run_cmd("gitea.example.com")
            with mock.patch.object(forge_api, "is_gitea", return_value=False), \
                    self.assertRaises(Die):
                self.run_cmd("gitlab.com")


class TestManage(unittest.TestCase):
    """`forge connect`, `list`, `show` and `remove` from the CLI, through the
    host controller."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.root = self.tmp / "root"
        self.addCleanup(use_bottle_root(self.root))
        serve_host_controller(self)
        as_cli(self)
        self.enterContext(mock.patch.object(forge_api, "is_gitea", return_value=True))
        self.check = self.enterContext(mock.patch.object(
            forge_api, "check_token",
            return_value=forge_api.TokenCheck("bot", "ssh://git@10.0.0.1:30009", 1, 1)))
        self.enterContext(mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                                     return_value="ssh-ed25519 AAAA"))

    def forge(self, *argv: str, token: str = "s3cret-token\n") -> str:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(token)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cmd_forge(list(argv)))
        return out.getvalue()

    def test_connect_writes_through_the_host_controller(self) -> None:
        self.assertIn("no forges connected", self.forge("list"))
        self.forge("connect", "gitea.example.com", "--token-stdin")
        self.check.return_value = forge_api.TokenCheck("codex", "", 1, 1)
        self.forge("connect", "gitea.example.com", "--token-stdin", token="other\n")
        conn = ForgeStore(host_db_path()).connections()["gitea.example.com"]
        self.assertEqual(["bot", "codex"], [u.name for u in conn.users])
        self.assertEqual("gitea.example.com            gitea   bot*, codex\n", self.forge("list"))
        shown = self.forge("show", "gitea.example.com")
        self.assertIn("ssh       ssh://git@gitea.example.com", shown)  # codex reported none
        self.assertIn("host key  ssh-ed25519 AAAA", shown)
        self.assertIn("user      bot (token secret:BB_HOST_FORGE_GITEA_EXAMPLE_COM__BOT_TOKEN, "
                      "default)", shown)

    def test_remove_a_user_or_the_forge(self) -> None:
        self.forge("connect", "gitea.example.com", "--token-stdin")
        self.check.return_value = forge_api.TokenCheck("codex", "", 1, 1)
        self.forge("connect", "gitea.example.com", "--token-stdin", token="other\n")
        self.forge("remove", "gitea.example.com", "--user", "codex")
        secrets = (self.root / "secrets").read_text()
        self.assertNotIn("BB_HOST_FORGE_GITEA_EXAMPLE_COM__CODEX_TOKEN", secrets)
        self.assertIn("BB_HOST_FORGE_GITEA_EXAMPLE_COM__BOT_TOKEN=s3cret-token", secrets)
        self.forge("remove", "gitea.example.com")
        self.assertEqual({}, ForgeStore(host_db_path()).connections())
        self.assertNotIn("BB_HOST_FORGE_GITEA_EXAMPLE_COM__BOT_TOKEN",
                         (self.root / "secrets").read_text())

    def test_refusals(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["show", "nowhere.example.com"], ["remove", "nowhere.example.com"],
                         ["show"], ["remove"], ["list", "x"], ["remove", "h", "--user"]):
                with self.subTest(argv=argv), self.assertRaises(Die):
                    cmd_forge(argv)


if __name__ == "__main__":
    unittest.main()
