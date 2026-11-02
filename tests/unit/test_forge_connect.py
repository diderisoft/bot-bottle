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
from bot_bottle.forge_connect import (
    ForgeEntry, bottle_path, default_key, existing_forges, ssh_host_port, write_forge,
)
from bot_bottle.lib.log import Die
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from bot_bottle.manifest.util import ManifestError
from bot_bottle.manifest.versions import history
from tests.unit import use_bottle_root

ENTRY = ForgeEntry(key="gitea.example.com", kind="gitea", url="https://gitea.example.com",
                   ssh="ssh://git@10.0.0.1:30009", account="bot", host_key="ssh-ed25519 AAAA")


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with


class TestEntry(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual("GITEA_EXAMPLE_COM_TOKEN", ENTRY.secret_name)
        self.assertEqual("gitea-example-com", ENTRY.label)
        other = ForgeEntry(key="codex@10.0.0.1:3000", kind="gitea", url="http://10.0.0.1:3000",
                           ssh="", account="codex", host_key="")
        self.assertEqual("CODEX_10_0_0_1_3000_TOKEN", other.secret_name)
        self.assertEqual("codex-10-0-0-1-3000", other.label)
        ip = ForgeEntry(key="10.0.0.1", kind="gitea", url="http://10.0.0.1", ssh="",
                        account="", host_key="")
        self.assertEqual("forge-10-0-0-1", ip.label)

    def test_default_key(self) -> None:
        self.assertEqual("gitea.example.com", default_key("https://gitea.example.com", "bot", {}))
        self.assertEqual("g.example.com:3000", default_key("http://g.example.com:3000", "bot", {}))
        taken = {"gitea.example.com": {"account": "someone"}}
        self.assertEqual("bot@gitea.example.com",
                         default_key("https://gitea.example.com", "bot", taken))
        mine = {"gitea.example.com": {"account": "bot"}}
        self.assertEqual("gitea.example.com",
                         default_key("https://gitea.example.com", "bot", mine))

    def test_ssh_host_port(self) -> None:
        self.assertEqual(("10.0.0.1", "30009"), ssh_host_port("ssh://git@10.0.0.1:30009"))
        self.assertEqual(("h", "22"), ssh_host_port("ssh://git@h"))


class TestWriteForge(_Tmp):
    def test_new_bottle_loads_and_grants_resolve(self) -> None:
        write_forge(self.tmp / "gitea.md", ENTRY)
        (self.tmp / "dev.md").write_text(
            "---\nextends: [gitea, basic]\ngit-gate:\n  repos: [gitea.example.com/a/b]\n---\n")
        bottle, _ = load_bottle_chain("dev", [(self.tmp, False), (BUILTIN_BOTTLES_DIR, False)])
        self.assertEqual("ssh://git@10.0.0.1:30009/a/b.git", bottle.git[0].Upstream)
        self.assertEqual("GITEA_EXAMPLE_COM_TOKEN", bottle.git[0].Key.forge_token_env)
        self.assertTrue(bottle.git[0].Key.read_only)
        self.assertIn("gitea.example.com", [r.Host for r in bottle.egress.routes])

    def test_adds_to_an_existing_bottle(self) -> None:
        path = self.tmp / "work.md"
        path.write_text("---\negress:\n  routes:\n    - host: gitea.example.com\n---\n\nMine.\n")
        write_forge(path, ENTRY)
        second = ForgeEntry(key="codex@gitea.example.com", kind="gitea",
                            url=ENTRY.url, ssh=ENTRY.ssh, account="codex", host_key="")
        write_forge(path, second)
        text = path.read_text()
        self.assertTrue(text.endswith("Mine.\n"))
        self.assertEqual(1, text.count("- host: gitea.example.com"))  # its route kept
        self.assertEqual({"gitea.example.com", "codex@gitea.example.com"},
                         set(existing_forges(path)))

    def test_refuses_a_malformed_section(self) -> None:
        path = self.tmp / "bad.md"
        path.write_text("---\ncredentials: [x]\n---\n")
        with self.assertRaises(ManifestError):
            write_forge(path, ENTRY)

    def test_bottle_path(self) -> None:
        self.assertEqual((self.tmp / "h" / "gitea.md", False),
                         bottle_path("gitea", host_bottles=self.tmp / "h", cwd=self.tmp))
        self.assertEqual((self.tmp / ".bot-bottle" / "bottles" / "main.md", True),
                         bottle_path("./main", host_bottles=self.tmp / "h", cwd=self.tmp))
        with self.assertRaises(ManifestError):
            bottle_path("../x", host_bottles=self.tmp, cwd=self.tmp)


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


class TestCommand(_Tmp):
    def setUp(self) -> None:
        super().setUp()
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

    def test_writes_the_forge_bottle_and_the_secret(self) -> None:
        self.run_cmd("gitea.example.com")
        bottle = self.root / "bottles" / "gitea.md"
        self.assertIn("token: $gitea-example-com", bottle.read_text())
        self.assertNotIn("s3cret", bottle.read_text())
        secrets = (self.root / "secrets").read_text()
        self.assertIn("GITEA_EXAMPLE_COM_TOKEN=s3cret-token", secrets)
        self.assertEqual("forge connect: gitea.example.com in gitea",
                         history(bottle, root=self.root)[0].message)
        self.check.assert_called_once_with("https://gitea.example.com", "s3cret-token")

    def test_ssh_override(self) -> None:
        self.run_cmd("gitea.example.com", "--ssh", "ssh://git@100.64.0.1:30009")
        forge = existing_forges(self.root / "bottles" / "gitea.md")["gitea.example.com"]
        self.assertEqual("ssh://git@100.64.0.1:30009", forge["ssh"])  # type: ignore[index]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Die):
            self.run_cmd("gitea.example.com", "--ssh", "100.64.0.1")

    def test_into_a_named_bottle_under_a_name(self) -> None:
        self.run_cmd("https://gitea.example.com/", "work", "codex@gitea.example.com")
        self.assertEqual({"codex@gitea.example.com"},
                         set(existing_forges(self.root / "bottles" / "work.md")))

    def test_into_a_project_bottle_trusts_it(self) -> None:
        with mock.patch("bot_bottle.approvals.trust_project_bottle") as trust:
            self.run_cmd("gitea.example.com", "./main")
        self.assertTrue((self.tmp / ".bot-bottle" / "bottles" / "main.md").is_file())
        trust.assert_called_once_with(self.tmp.resolve(), "main")

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
