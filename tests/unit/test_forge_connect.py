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
from bot_bottle.forge_connect import ssh_host_port
from bot_bottle.forge_connections import load
from bot_bottle.lib.log import Die
from tests.unit import use_bottle_root

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
        text = (self.root / "forges.md").read_text()
        self.assertNotIn("s3cret", text)
        self.assertFalse((self.root / "bottles" / "gitea.md").exists())
        conn = load(self.root / "forges.md")["gitea.example.com"]
        self.assertEqual(("gitea", "https://gitea.example.com", "ssh://git@10.0.0.1:30009",
                          "ssh-ed25519 AAAA"), (conn.kind, conn.url, conn.ssh, conn.host_key))
        self.assertEqual([("bot", "GITEA_EXAMPLE_COM_TOKEN", True)],
                         [(u.name, u.token_secret, u.default) for u in conn.users])
        secrets = (self.root / "secrets").read_text()
        self.assertIn("GITEA_EXAMPLE_COM_TOKEN=s3cret-token", secrets)
        self.check.assert_called_once_with("https://gitea.example.com", "s3cret-token")

    def test_another_user_and_the_default(self) -> None:
        self.run_cmd("gitea.example.com")
        self.check.return_value = forge_api.TokenCheck("codex", "", 1, 1)
        self.run_cmd("gitea.example.com", token="other\n")
        users = load(self.root / "forges.md")["gitea.example.com"].users
        self.assertEqual([("bot", "GITEA_EXAMPLE_COM_TOKEN", True),
                          ("codex", "CODEX_GITEA_EXAMPLE_COM_TOKEN", False)],
                         [(u.name, u.token_secret, u.default) for u in users])
        self.run_cmd("gitea.example.com", "--default", token="other\n")
        users = load(self.root / "forges.md")["gitea.example.com"].users
        self.assertEqual({"codex"}, {u.name for u in users if u.default})
        self.assertEqual("CODEX_GITEA_EXAMPLE_COM_TOKEN", users[-1].token_secret)

    def test_ssh_override(self) -> None:
        self.run_cmd("gitea.example.com", "--ssh", "ssh://git@100.64.0.1:30009")
        conn = load(self.root / "forges.md")["gitea.example.com"]
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


if __name__ == "__main__":
    unittest.main()
