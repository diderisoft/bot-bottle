"""The forges a host connected: `~/.bot-bottle/forges.md` (ADR 0019)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.forge_connections import (
    ForgeConnection, ForgeConnectionError, ForgeUser, add_user, host_of, load, save, secret_name,
)

TEXT = """---
gitea.example.com:
  kind: gitea
  url: https://gitea.example.com/
  ssh: ssh://git@10.0.4.2:30009
  host_key: ssh-ed25519 AAAA
  users:
    alice:
      token: $GITEA_EXAMPLE_COM_TOKEN
      default: true
    codex:
      token: $CODEX_GITEA_EXAMPLE_COM_TOKEN
github.com:
  kind: github
  url: https://github.com
  users:
    alice: { token: $GITHUB_COM_TOKEN, app: bot-bottle, transport: deploy-key }
---
"""


class TestLoad(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.path = self.tmp / "forges.md"

    def write(self, text: str) -> None:
        self.path.write_text(text)

    def test_reads_forges_and_users(self) -> None:
        self.write(TEXT)
        found = load(self.path)
        gitea, github = found["gitea.example.com"], found["github.com"]
        self.assertEqual(("https://gitea.example.com", "ssh://git@10.0.4.2:30009"),
                         (gitea.url, gitea.ssh))
        self.assertEqual("alice", gitea.user().name)
        self.assertEqual("CODEX_GITEA_EXAMPLE_COM_TOKEN", gitea.user("codex").token_secret)
        self.assertEqual("ssh://git@github.com", github.ssh)  # the default
        self.assertEqual(("bot-bottle", "deploy-key"),
                         (github.user().app, github.user().transport))
        self.assertEqual({"kind": "gitea", "url": "https://gitea.example.com",
                          "ssh": "ssh://git@10.0.4.2:30009", "host_key": "ssh-ed25519 AAAA",
                          "user": "alice", "transport": ""}, gitea.snapshot(gitea.user()))
        with self.assertRaisesRegex(ForgeConnectionError, "nobody isn't connected"):
            gitea.user("nobody")

    def test_a_token_is_secret_name_and_the_old_dollar_form_still_reads(self) -> None:
        self.write(TEXT.replace("token: $GITEA_EXAMPLE_COM_TOKEN",
                                "token: secret:GITEA_EXAMPLE_COM_TOKEN"))
        found = load(self.path)
        self.assertEqual("GITEA_EXAMPLE_COM_TOKEN", found["gitea.example.com"].user().token_secret)
        self.assertEqual("CODEX_GITEA_EXAMPLE_COM_TOKEN",  # written as $NAME
                         found["gitea.example.com"].user("codex").token_secret)
        save(found, self.path)
        text = self.path.read_text()
        self.assertIn("token: secret:CODEX_GITEA_EXAMPLE_COM_TOKEN", text)
        self.assertNotIn("$", text)
        self.assertEqual(found, load(self.path))

    def test_a_token_must_name_a_secret(self) -> None:
        for bad in ("secret:", "secret:a b", "plain", "?"):
            with self.subTest(bad=bad):
                self.write(TEXT.replace("$GITEA_EXAMPLE_COM_TOKEN", bad))
                with self.assertRaisesRegex(ForgeConnectionError, "as secret:NAME"):
                    load(self.path)

    def test_none_connected(self) -> None:
        self.assertEqual({}, load(self.path))

    def test_refusals(self) -> None:
        forge = "gitea.example.com:\n  kind: gitea\n  url: https://gitea.example.com\n"
        users = forge + "  users:\n"
        cases = {
            "gitea.example.com: {kind: gitea, url: https://other.example.com}": "must be on",
            "gitea.example.com: {kind: gitlab, url: https://gitea.example.com}": "kind must",
            forge + "  x: 1": "unknown key 'x'",
            '"a b": {}': "keyed by its host",
            users + "    a: {token: T}": "names a host secret",
            users + "    a: {token: $T, transport: https}": "transport must be deploy-key",
            users + "    a: {token: $T, app: x}": "app is for GitHub",
            users + "    a: {token: $T, default: true}\n    b: {token: $U, default: true}":
                "only one user",
        }
        for body, fragment in cases.items():
            with self.subTest(body=body):
                self.write(f"---\n{body}\n---\n")
                with self.assertRaisesRegex(ForgeConnectionError, fragment):
                    load(self.path)

    def test_no_default_among_several(self) -> None:
        self.write("---\ngitea.example.com:\n  kind: gitea\n  url: https://gitea.example.com\n"
                   "  users:\n    a: {token: $T}\n    b: {token: $U}\n---\n")
        with self.assertRaisesRegex(ForgeConnectionError, "none is the default"):
            load(self.path)["gitea.example.com"].user()


class TestWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.path = self.tmp / "forges.md"

    def test_round_trip(self) -> None:
        self.path.write_text(TEXT)
        found = load(self.path)
        save(found, self.path)
        self.assertEqual(found, load(self.path))
        self.assertIn("forge connect", self.path.read_text())

    def test_add_user(self) -> None:
        conn = ForgeConnection(host="h.example.com", kind="gitea", url="https://h.example.com",
                               ssh="ssh://git@h.example.com")
        first = add_user(conn, ForgeUser("a", "H_EXAMPLE_COM_TOKEN"), path=self.path)
        self.assertTrue(first.users[0].default)
        second = add_user(conn, ForgeUser("b", "B_H_EXAMPLE_COM_TOKEN"), path=self.path)
        self.assertEqual([("a", True), ("b", False)], [(u.name, u.default) for u in second.users])
        again = add_user(conn, ForgeUser("a", "H_EXAMPLE_COM_TOKEN"), path=self.path)
        self.assertEqual({"a"}, {u.name for u in again.users if u.default})
        made = add_user(conn, ForgeUser("b", "B_H_EXAMPLE_COM_TOKEN"), make_default=True,
                        path=self.path)
        self.assertEqual({"b"}, {u.name for u in made.users if u.default})
        self.assertEqual(made, load(self.path)["h.example.com"])

    def test_names(self) -> None:
        self.assertEqual("GITEA_EXAMPLE_COM_TOKEN",
                         secret_name("gitea.example.com", "bot", first=True))
        self.assertEqual("CODEX_10_0_0_1_3000_TOKEN", secret_name("10.0.0.1:3000", "codex",
                                                                 first=False))
        self.assertEqual("g.example.com:3000", host_of("http://g.example.com:3000/x"))


if __name__ == "__main__":
    unittest.main()
