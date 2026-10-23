"""Unit: host login detection for the starters, and the secrets-file writer."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.contrib.claude import claude_auth
from bot_bottle.contrib.codex.codex_auth import codex_login_location
from bot_bottle.contrib.pi.starter import reachable_from_bottle
from bot_bottle.errors import SecretsFileError
from bot_bottle.secret_source import FileSecrets, write_secret


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)


class ClaudeLogin(_Tmp):
    def test_the_credentials_file_wins(self) -> None:
        creds = self.dir / ".claude" / ".credentials.json"
        creds.parent.mkdir()
        creds.write_text("{}")
        self.assertEqual(str(creds), claude_auth.claude_login_location({"HOME": str(self.dir)}))

    def test_the_keychain_is_checked_without_reading_the_secret(self) -> None:
        done = subprocess.CompletedProcess([], 0)
        with patch.object(claude_auth.sys, "platform", "darwin"), \
                patch.object(claude_auth.subprocess, "run", return_value=done) as run:
            location = claude_auth.claude_login_location({"HOME": str(self.dir)})
        self.assertIn("Keychain", location)
        self.assertNotIn("-w", run.call_args.args[0])

    def test_nothing_found(self) -> None:
        with patch.object(claude_auth.sys, "platform", "linux"):
            self.assertEqual("", claude_auth.claude_login_location({"HOME": str(self.dir)}))


class CodexLogin(_Tmp):
    def write(self, payload: object) -> dict[str, str]:
        (self.dir / "auth.json").write_text(json.dumps(payload))
        return {"CODEX_HOME": str(self.dir)}

    def test_a_device_login_counts(self) -> None:
        env = self.write({"auth_mode": "chatgpt", "tokens": {"access_token": "t"}})
        self.assertEqual(str(self.dir / "auth.json"), codex_login_location(env))

    def test_an_api_key_login_or_no_token_does_not(self) -> None:
        for payload in ({"auth_mode": "api_key", "tokens": {"access_token": "t"}},
                        {"auth_mode": "chatgpt", "tokens": {}}, ["not", "an", "object"]):
            with self.subTest(payload=payload):
                self.assertEqual("", codex_login_location(self.write(payload)))


class Reachability(unittest.TestCase):
    def test_only_public_https_is_reachable_from_a_bottle(self) -> None:
        self.assertTrue(reachable_from_bottle("https://openrouter.ai/api/v1"))
        for url in ("http://openrouter.ai/v1", "https://localhost:1/v1", "https://127.0.0.1/v1",
                    "https://192.168.1.5/v1", "https://box.local/v1"):
            with self.subTest(url=url):
                self.assertFalse(reachable_from_bottle(url))


class WriteSecret(_Tmp):
    def test_appends_replaces_and_keeps_the_file_private(self) -> None:
        path = self.dir / "secrets"
        path.write_text("# mine\nKEEP=1\nNAME=old\n")
        os.chmod(path, 0o600)
        write_secret(path, "NAME", "new")
        write_secret(path, "OTHER", "x")
        self.assertEqual("# mine\nKEEP=1\nNAME=new\nOTHER=x\n", path.read_text())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual("new", FileSecrets(path).get("NAME"))

    def test_refuses_bad_names_and_multiline_values(self) -> None:
        for name, value in (("bad name", "v"), ("NAME", "a\nb"), ("NAME", "")):
            with self.subTest(name=name), self.assertRaises(SecretsFileError):
                write_secret(self.dir / "secrets", name, value)


if __name__ == "__main__":
    unittest.main()
