"""Bottles in the old git-gate format (long-form repos with forge-minted keys):
the load error that explains the fix, and `bottles migrate` (ADR 0008)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.cli.commands import bottle as bottle_cmd
from bot_bottle.forge_connections import ForgeConnection, ForgeUser, save
from bot_bottle.manifest import ManifestError, ManifestIndex
from bot_bottle.manifest.legacy_git_gate import OldGitGateFormatError
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from bot_bottle.manifest.migrate import MigrateError, apply, prepare
from bot_bottle.manifest.versions import history, revert

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOldHostKey"


def _old_repo(repo: str, *, read_only: bool = False, host: str = "10.0.0.5:30009",
              api: str = "https://gitea.example.com") -> str:
    ro = ", read_only: true" if read_only else ""
    return (f"    {repo}:\n"
            f"      url: ssh://git@{host}/acme/{repo}.git\n"
            f"      host_key: {HOST_KEY}\n"
            f"      key: {{ provider: gitea, forge_token_env: GITEA_ADMIN_TOKEN, "
            f"api_url: {api}{ro} }}\n")


OLD = ("---\n"
       "# the dev bottle\n"
       "extends: basic   # registries\n"
       "\n"
       "env:\n"
       "  FOO: bar\n"
       "\n"
       "git-gate:\n"
       "  user:\n"
       "    name: dev-bot\n"
       "    email: bot@example.com\n"
       "  repos:\n"
       + _old_repo("bot-bottle")
       + "    # the brand repo\n"
       + _old_repo("bot-bottle-brand")
       + _old_repo("textorbium", read_only=True)
       + "\n"
       "egress:\n"
       "  routes:\n"
       "    - host: api.example.com  # keep me\n"
       "---\n"
       "\n"
       "The dev bottle.\n")

CONN = ForgeConnection(host="gitea.example.com", kind="gitea", url="https://gitea.example.com",
                       ssh="ssh://git@10.0.0.5:30009", host_key=HOST_KEY,
                       users=(ForgeUser("bot", "GITEA_EXAMPLE_COM_TOKEN", default=True),))


class _Host(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.root = Path(tmp) / ".bot-bottle"
        self.bottles = self.root / "bottles"
        self.bottles.mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)}))
        self.dirs = [(self.bottles, False), (BUILTIN_BOTTLES_DIR, False)]

    def connect(self) -> None:
        save({CONN.host: CONN}, self.root / "forges.md")

    def write(self, name: str, text: str) -> Path:
        path = self.bottles / f"{name}.md"
        path.write_text(text)
        return path

    def load_error(self, name: str = "dev") -> str:
        with self.assertRaises(OldGitGateFormatError) as ctx:
            load_bottle_chain(name, self.dirs)
        return str(ctx.exception)


class TestTheError(_Host):
    def test_names_every_old_repo_at_once(self) -> None:
        self.write("dev", OLD)
        msg = self.load_error()
        self.assertIn("bottle 'dev' uses the old git-gate format", msg)
        self.assertIn("(bot-bottle, bot-bottle-brand, textorbium)", msg)
        self.assertIn("bot-bottle bottles migrate dev", msg)
        self.assertIn("git-gate.md#migrating-from-the-old-format", msg)

    def test_the_forge_is_the_api_host_not_the_ssh_ip(self) -> None:
        self.write("dev", OLD)
        msg = self.load_error()
        self.assertIn("- gitea.example.com/acme/bot-bottle:write", msg)
        self.assertNotIn("10.0.0.5/acme", msg)
        # The IP is what reaches SSH, so connecting keeps it.
        self.assertIn("bot-bottle forge connect https://gitea.example.com "
                      "--ssh ssh://git@10.0.0.5:30009", msg)

    def test_read_only_stays_read_only(self) -> None:
        self.write("dev", OLD)
        msg = self.load_error()
        self.assertIn("- gitea.example.com/acme/textorbium\n", msg)
        self.assertNotIn("textorbium:write", msg)

    def test_hand_edit_adds_no_extends(self) -> None:
        self.write("dev", OLD)
        self.assertNotIn("extends", self.load_error())

    def test_a_connected_forge_is_named(self) -> None:
        self.write("dev", OLD)
        self.connect()
        msg = self.load_error()
        self.assertIn("gitea.example.com is connected already", msg)
        self.assertNotIn("forge connect", msg)

    def test_no_secret_values(self) -> None:
        self.write("dev", OLD)
        msg = self.load_error()
        self.assertNotIn(HOST_KEY, msg)
        self.assertNotIn("forge_token_env", msg)

    def test_from_a_json_manifest_too(self) -> None:
        with self.assertRaises(ManifestError) as ctx:
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"git-gate": {"repos": {
                    "a": {"url": "ssh://git@10.0.0.1:2222/org/a.git",
                          "key": {"provider": "gitea", "forge_token_env": "T",
                                  "api_url": "https://git.example.com"}},
                    "b": {"url": "ssh://git@10.0.0.1:2222/org/b.git",
                          "key": {"provider": "gitea", "forge_token_env": "T",
                                  "api_url": "https://git.example.com"}},
                    "lib": {"url": "ssh://git@github.com/org/lib.git",
                            "key": {"provider": "static", "path": "/dev/null"}}}}}},
                "agents": {"demo": {"skills": [], "prompt": ""}},
            })
        msg = str(ctx.exception)
        self.assertIn("(a, b)", msg)
        self.assertIn("a: git.example.com/org/a:write", msg)
        self.assertIn("lib as they are", msg)


class TestMigrate(_Host):
    def test_refused_without_a_connection(self) -> None:
        path = self.write("dev", OLD)
        with self.assertRaises(MigrateError) as ctx:
            prepare("dev", self.dirs)
        msg = str(ctx.exception)
        self.assertIn("the forge gitea.example.com isn't connected on this host yet", msg)
        self.assertIn("bot-bottle forge connect https://gitea.example.com", msg)
        self.assertEqual(OLD, path.read_text())

    def test_rewrites_only_the_repos(self) -> None:
        self.write("dev", OLD)
        self.connect()
        m = prepare("dev", self.dirs)
        expected = OLD
        start = expected.index("  repos:\n") + len("  repos:\n")
        end = expected.index("\negress:")
        expected = (expected[:start]
                    + "    - gitea.example.com/acme/bot-bottle:write\n"
                    + "    - gitea.example.com/acme/bot-bottle-brand:write\n"
                    + "    - gitea.example.com/acme/textorbium\n"
                    + expected[end:])
        self.assertEqual(expected, m.after)
        apply(m, self.dirs, root=self.root)
        bottle, _ = load_bottle_chain("dev", self.dirs)
        self.assertEqual(
            [("bot-bottle", False), ("bot-bottle-brand", False), ("textorbium", True)],
            [(e.Name, e.Key.read_only) for e in bottle.git])
        self.assertEqual("ssh://git@10.0.0.5:30009/acme/bot-bottle.git",
                         bottle.git[0].Upstream)
        self.assertEqual(HOST_KEY, bottle.git[0].KnownHostKey)
        self.assertEqual({"FOO": "bar"}, bottle.env)

    def test_a_static_repo_and_a_renamed_one_keep_the_mapping_form(self) -> None:
        text = OLD.replace("    textorbium:\n", "    orb:\n").replace(
            "  repos:\n", "  repos:\n    lib:\n      url: ssh://git@github.com/o/lib.git\n"
                          "      key: { provider: static, path: /dev/null }\n")
        self.write("dev", text)
        self.connect()
        after = prepare("dev", self.dirs).after
        self.assertIn("    lib:\n      url: ssh://git@github.com/o/lib.git\n", after)
        self.assertIn("    bot-bottle: gitea.example.com/acme/bot-bottle:write\n", after)
        self.assertIn("    orb: gitea.example.com/acme/textorbium\n", after)
        self.assertIn("    # the brand repo\n", after)

    def test_an_unusual_layout_is_refused(self) -> None:
        self.connect()
        self.write("dev", "---\ngit-gate:\n  repos: {a: {url: 'ssh://git@h/o/a.git', "
                          "key: {provider: gitea, forge_token_env: T, "
                          "api_url: 'https://gitea.example.com'}}}\n---\n")
        with self.assertRaises(MigrateError):
            prepare("dev", self.dirs)

    def test_nothing_to_migrate(self) -> None:
        self.connect()
        self.write("plain", "---\nextends: basic\n---\n")
        with self.assertRaises(MigrateError):
            prepare("plain", self.dirs)

    def test_notes_a_different_token(self) -> None:
        self.write("dev", OLD)
        self.connect()
        notes = prepare("dev", self.dirs).notes
        self.assertTrue(any("GITEA_EXAMPLE_COM_TOKEN" in n and "GITEA_ADMIN_TOKEN" in n
                            for n in notes))

    def test_a_revert_restores_the_old_file(self) -> None:
        path = self.write("dev", OLD)
        self.connect()
        sha = apply(prepare("dev", self.dirs), self.dirs, root=self.root)
        versions = history(path, root=self.root)
        self.assertEqual(sha, versions[0].sha)
        self.assertEqual("bottles migrate: dev to forge grants", versions[0].message)
        revert(path, versions[1].sha[:9], root=self.root)
        self.assertEqual(OLD, path.read_text())


class TestMigrateCommand(_Host):
    def run_cmd(self, argv: list[str], *, tty: bool = False,
                answer: str = "") -> tuple[int, str]:
        out = io.StringIO()
        stdin = io.StringIO(answer)
        stdin.isatty = lambda: tty  # type: ignore[method-assign]
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()), \
                patch("sys.stdin", stdin), \
                patch("bot_bottle.cli.commands.bottle.os.getcwd", return_value=str(self.root)), \
                patch("bot_bottle.manifest.index.ManifestIndex.bottle_dirs",
                      return_value=self.dirs):
            try:
                code = bottle_cmd.cmd_bottle(argv)
            except SystemExit as e:
                code = int(e.code or 0)
        return code, out.getvalue()

    def test_yes_writes_it(self) -> None:
        path = self.write("dev", OLD)
        self.connect()
        code, out = self.run_cmd(["migrate", "dev", "--yes"])
        self.assertEqual(0, code)
        self.assertIn("+    - gitea.example.com/acme/bot-bottle:write", out)
        self.assertIn("gitea.example.com/acme/bot-bottle:write", path.read_text())

    def test_without_a_terminal_it_needs_yes(self) -> None:
        path = self.write("dev", OLD)
        self.connect()
        self.assertEqual(1, self.run_cmd(["migrate", "dev"])[0])
        self.assertEqual(OLD, path.read_text())

    def test_declining_leaves_it(self) -> None:
        path = self.write("dev", OLD)
        self.connect()
        with patch("builtins.input", return_value="n"):
            self.assertEqual(1, self.run_cmd(["migrate", "dev"], tty=True)[0])
        self.assertEqual(OLD, path.read_text())


if __name__ == "__main__":
    unittest.main()
