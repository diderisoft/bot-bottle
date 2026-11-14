"""Plain SSH servers are forges of kind `ssh` (ADR 0023): their connection
holds key files by path and fingerprint, grants use them, and a bottle file
holds no long-form repo."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle import forge_connections, forge_keys
from bot_bottle.cli.commands.forge import cmd_forge
from bot_bottle.contract import forges as contract_forges
from bot_bottle.control.hostctl import forge_link, host_state
from bot_bottle.forge_connect import ConnectError, connect, connect_ssh
from bot_bottle.forge_connections import (
    ForgeConnection, ForgeConnectionError, ForgeKey, ForgeStore, ForgeUser,
)
from bot_bottle.forge_kinds import forge_api
from bot_bottle.lib.log import Die
from bot_bottle.manifest.extends import resolve_bottles
from bot_bottle.manifest.forges import (
    MissingKeyError, described_connection, forges_raw, resolve_forge,
)
from bot_bottle.manifest.git import ManifestGitEntry
from bot_bottle.manifest.legacy_git_gate import OldGitGateFormatError
from bot_bottle.manifest.parse_check import problem
from bot_bottle.manifest.trust import forge_lines, grants
from bot_bottle.manifest.util import ManifestError
from bot_bottle.paths import host_db_path
from tests.unit import as_cli, serve_host_controller, use_bottle_root
from tests.unit._forges import GITEA, SSH, connected

HAS_KEYGEN = shutil.which("ssh-keygen") is not None
DECLARED: Any = {"git_gate": {"forges": {"git.example.com": {
    "kind": "ssh", "ssh": "ssh://git@git.example.com:2222"}}}}


def _keygen(path: Path) -> str:
    """A fresh unencrypted ed25519 key at `path` (0600); its fingerprint."""
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                   check=True)
    out = subprocess.run(["ssh-keygen", "-l", "-f", str(path)], check=True, text=True,
                         capture_output=True).stdout
    return out.split()[1]


class TestKinds(unittest.TestCase):
    def test_ssh_is_a_kind_without_an_api(self) -> None:
        self.assertIn("ssh", contract_forges.FORGE_KINDS)
        self.assertNotIn("ssh", contract_forges.API_KINDS)
        self.assertEqual("Gitea, GitHub", contract_forges.supported())
        with self.assertRaises(ValueError):
            forge_api("ssh")


class TestConnectionJson(unittest.TestCase):
    def test_round_trip(self) -> None:
        self.assertEqual(SSH, ForgeConnection.from_json(SSH.to_json()))

    def test_keys_and_their_pick(self) -> None:
        self.assertEqual("/keys/lib", (SSH.key_for("org/lib") or SSH).path)  # type: ignore
        self.assertEqual("/keys/default", (SSH.key_for("org/other") or SSH).path)  # type: ignore
        self.assertIsNone(replace(SSH, keys=SSH.keys[1:]).key_for("org/other"))

    def test_an_ssh_server_has_no_users(self) -> None:
        with self.assertRaisesRegex(ForgeConnectionError, "it has keys, not users"):
            SSH.user()

    def test_refused_shapes(self) -> None:
        base = SSH.to_json()
        for change, why in (
                ({"url": "https://git.example.com"}, "no URL and no users"),
                ({"users": [{"name": "a", "token": "secret:T"}]}, "no URL and no users"),
                ({"ssh": "ssh://git@other.example.com"}, "ssh must be"),
                ({"ssh": "https://git.example.com"}, "ssh must be"),
                ({"keys": [{"repo": "x", "path": "/k", "fingerprint": "f"}]}, "<owner>/<repo>"),
                ({"keys": [{"repo": "", "path": "k", "fingerprint": "f"}]}, "absolute"),
                ({"keys": [{"repo": "", "path": "/k", "fingerprint": ""}]}, "no fingerprint"),
                ({"keys": [{"repo": "", "path": "/k", "fingerprint": "f", "write": 1}]},
                 "true or false"),
                ({"keys": [{"repo": "", "path": "/k", "fingerprint": "f"}] * 2}, "one key"),
                ({"keys": "x"}, "must be a list")):
            with self.subTest(change=change), self.assertRaisesRegex(ForgeConnectionError, why):
                ForgeConnection.from_json({**base, **change})

    def test_only_an_ssh_server_holds_keys(self) -> None:
        with self.assertRaisesRegex(ForgeConnectionError, "only an SSH server"):
            ForgeConnection.from_json({**GITEA.to_json(), "keys": SSH.to_json()["keys"]})


class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))
        self.store = ForgeStore(host_db_path())
        self.bare = replace(SSH, keys=())

    def test_connect_add_remove(self) -> None:
        self.assertEqual((), self.store.connect_server(self.bare).keys)
        for key in SSH.keys:
            self.store.add_key(SSH.host, key)
        self.assertEqual(SSH, self.store.connections()[SSH.host])
        # Connecting again re-pins and keeps the keys.
        again = self.store.connect_server(replace(self.bare, host_key="ssh-ed25519 NEW"))
        self.assertEqual(("ssh-ed25519 NEW", SSH.keys), (again.host_key, again.keys))
        self.assertEqual(SSH.keys[1], self.store.remove_key(SSH.host, "org/lib"))
        with self.assertRaisesRegex(ForgeConnectionError, "holds no key for org/lib"):
            self.store.remove_key(SSH.host, "org/lib")
        self.assertEqual([], self.store.remove(SSH.host))  # no users; the keys go too
        self.assertEqual({}, self.store.connections())
        self.store.connect_server(self.bare)
        self.assertEqual((), self.store.connections()[SSH.host].keys)

    def test_a_key_needs_an_ssh_server(self) -> None:
        with self.assertRaisesRegex(ForgeConnectionError, "connect it with"):
            self.store.add_key(SSH.host, SSH.keys[0])
        self.store.add_user(GITEA, GITEA.users[0])
        with self.assertRaisesRegex(ForgeConnectionError, "minted per launch"):
            self.store.add_key(GITEA.host, SSH.keys[0])

    def test_the_kind_stays(self) -> None:
        self.store.add_user(GITEA, GITEA.users[0])
        with self.assertRaisesRegex(ForgeConnectionError, "remove it first"):
            self.store.connect_server(replace(self.bare, host=GITEA.host,
                                              ssh=f"ssh://git@{GITEA.host}"))
        self.store.connect_server(self.bare)
        gitea_here = replace(GITEA, host=SSH.host, url=f"https://{SSH.host}")
        with self.assertRaisesRegex(ForgeConnectionError, "remove it first"):
            self.store.add_user(gitea_here, GITEA.users[0])
        with self.assertRaisesRegex(ForgeConnectionError, "without a user"):
            self.store.connect_server(GITEA)

    def test_refingerprint_changes_every_reference_to_the_file(self) -> None:
        other = replace(self.bare, host="other.example.com", ssh="ssh://git@other.example.com")
        for conn in (self.bare, other):
            self.store.connect_server(conn)
            self.store.add_key(conn.host, SSH.keys[0])
        self.assertEqual(2, self.store.refingerprint("/keys/default", "SHA256:new"))
        self.assertEqual(0, self.store.refingerprint("/keys/default", "SHA256:new"))
        self.assertEqual({"SHA256:new"}, {c.keys[0].fingerprint
                                          for c in self.store.connections().values()})
        with self.assertRaises(ForgeConnectionError):
            self.store.refingerprint("", "x")


@unittest.skipUnless(HAS_KEYGEN, "needs ssh-keygen")
class TestKeyFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.key = self.dir / "id"
        self.fp = _keygen(self.key)

    def test_check_file(self) -> None:
        self.assertEqual(self.fp, forge_keys.check_file(str(self.key)))
        self.assertTrue(self.fp.startswith("SHA256:"))
        with self.assertRaisesRegex(forge_keys.KeyFileError, "isn't there"):
            forge_keys.check_file(str(self.dir / "gone"))
        with self.assertRaisesRegex(forge_keys.KeyFileError, "isn't a file"):
            forge_keys.check_file(str(self.dir))
        self.key.chmod(0o640)
        with self.assertRaisesRegex(forge_keys.KeyFileError, "readable by others.*chmod 600"):
            forge_keys.check_file(str(self.key))
        junk = self.dir / "junk"
        junk.write_text("not a key")
        junk.chmod(0o600)
        with self.assertRaisesRegex(forge_keys.KeyFileError, "isn't a key ssh-keygen can read"):
            forge_keys.check_file(str(junk))

    def test_without_ssh_keygen(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError), \
                self.assertRaisesRegex(forge_keys.KeyFileError, "isn't installed"):
            forge_keys.fingerprint(str(self.key))
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)), \
                self.assertRaisesRegex(forge_keys.KeyFileError, "in time"):
            forge_keys.fingerprint(str(self.key))

    def _entry(self, fingerprint: str, path: str = "", read_only: bool = False
               ) -> ManifestGitEntry:
        return ManifestGitEntry.from_repos_entry("dev", "lib", {
            "url": "ssh://git@git.example.com:2222/org/lib.git",
            "key": {"provider": "ssh", "path": path or str(self.key),
                    "fingerprint": fingerprint, "read_only": read_only}})

    def test_a_launch_uses_a_rotated_key_and_records_it_once(self) -> None:
        with mock.patch.object(forge_connections, "refingerprint", return_value=1) as record, \
                mock.patch.object(forge_keys, "warn") as warn:
            forge_keys.check_launch([self._entry(self.fp)])
            record.assert_not_called()
            forge_keys.check_launch([self._entry("SHA256:old"), self._entry("SHA256:old")])
        record.assert_called_once_with(str(self.key), self.fp)
        warn.assert_called_once()
        self.assertIn("SHA256:old is now " + self.fp, warn.call_args.args[0])

    def test_a_record_that_fails_is_only_logged(self) -> None:
        with mock.patch.object(forge_connections, "refingerprint",
                               side_effect=ForgeConnectionError("down")), \
                mock.patch.object(forge_keys, "warn") as warn:
            forge_keys.check_launch([self._entry("SHA256:old")])
        self.assertIn("couldn't record", warn.call_args.args[0])

    def test_a_missing_key_fails_the_launch_with_the_fix(self) -> None:
        with self.assertRaises(forge_keys.KeyFileError) as ctx:
            forge_keys.check_launch([self._entry("f", str(self.dir / "gone"))])
        self.assertIn("the key for git.example.com/org/lib can't be used", str(ctx.exception))
        self.assertIn("`bot-bottle forge key add git.example.com org/lib <path> --write`",
                      str(ctx.exception))
        with self.assertRaises(forge_keys.KeyFileError) as ctx:
            forge_keys.check_launch([self._entry("f", str(self.dir / "gone"), read_only=True)])
        self.assertIn("org/lib <path>`", str(ctx.exception))

    def test_the_backend_refuses_the_launch(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.base import check_ssh_keys
        bottle = mock.Mock(git=(self._entry("f", str(self.dir / "gone")),))
        with self.assertRaises(Die):
            check_ssh_keys(bottle)

    def test_key_path(self) -> None:
        self.assertEqual("/work/k", forge_keys.key_path("k", "/work"))
        self.assertEqual("/abs/k", forge_keys.key_path("/abs/k", "/work"))


class TestDescriptions(unittest.TestCase):
    def _raw(self, **forge: object) -> dict[str, object]:
        return {"git_gate": {"forges": {"git.example.com": {"kind": "ssh", **forge}}}}

    def test_an_ssh_server_is_described_by_its_address(self) -> None:
        forges_raw("b", self._raw(ssh="ssh://git@git.example.com:2222", host_key="k"))
        forges_raw("b", self._raw())
        for forge, why in (({"url": "https://git.example.com"}, "has no url"),
                           ({"ssh": "ssh://git@other.example.com"}, "ssh must be"),
                           ({"ssh": "ssh://git.example.com"}, "ssh must be")):
            with self.subTest(forge=forge), self.assertRaisesRegex(ManifestError, why):
                forges_raw("b", self._raw(**forge))
        with self.assertRaisesRegex(ManifestError, "named by its host alone"):
            forges_raw("b", {"git_gate": {"forges": {"git.example.com:2222": {"kind": "ssh"}}}})

    def test_described_from_a_connection(self) -> None:
        self.assertEqual({"kind": "ssh", "ssh": "ssh://git@git.example.com:2222"},
                         described_connection(SSH))
        self.assertEqual({"kind": "ssh"}, described_connection(
            replace(SSH, ssh="ssh://git@git.example.com")))


@connected(SSH)
class TestGrants(unittest.TestCase):
    def _git(self, *wanted: str, **extra: Any) -> Any:
        raw: dict[str, object] = {
            **DECLARED, "git_gate": {**DECLARED["git_gate"], "repos": list(wanted)}, **extra}
        return resolve_bottles({"dev": raw})["dev"]

    def test_a_grant_uses_its_repos_key_else_the_default(self) -> None:
        bottle = self._git("git.example.com/org/lib", "git.example.com/org/app:write")
        lib, app = bottle.git
        self.assertEqual(("ssh", "/keys/lib", "SHA256:lib", True, "/keys/lib"),
                         (lib.Key.provider, lib.Key.path, lib.Key.fingerprint, lib.Key.read_only,
                          lib.IdentityFile))
        self.assertEqual(("/keys/default", False), (app.Key.path, app.Key.read_only))
        self.assertEqual("ssh://git@git.example.com:2222/org/app.git", app.Upstream)
        self.assertEqual("ssh-ed25519 SSH", app.KnownHostKey)
        self.assertEqual(frozenset(), bottle.undeclared_forges)

    def test_a_read_only_key_cant_take_a_write_grant(self) -> None:
        with self.assertRaises(MissingKeyError) as ctx:
            self._git("git.example.com/org/lib:write")
        self.assertIn("this host's key for git.example.com/org/lib is read-only, and the grant "
                      "is :write; add a write key with `bot-bottle forge key add "
                      "git.example.com org/lib <path> --write`", str(ctx.exception))

    def test_no_key_is_refused_naming_the_command(self) -> None:
        with connected(replace(SSH, keys=SSH.keys[1:])), \
                self.assertRaises(MissingKeyError) as ctx:
            self._git("git.example.com/org/app")
        self.assertEqual(("git.example.com", "org/app", False),
                         (ctx.exception.forge, ctx.exception.repo, ctx.exception.writable))
        self.assertEqual("bottle 'dev': this host holds no key for git.example.com/org/app; "
                         "add one with `bot-bottle forge key add git.example.com org/app "
                         "<path>` (or `bot-bottle forge key add git.example.com --default "
                         "<path>` for every repo there)", str(ctx.exception))

    def test_unconnected_names_the_ssh_connect(self) -> None:
        with connected(), self.assertRaisesRegex(
                ManifestError, "the forge git.example.com isn't connected on this host; connect "
                               "it with `bot-bottle forge connect "
                               "ssh://git@git.example.com:2222`"):
            self._git("git.example.com/org/app")

    def test_kind_must_match(self) -> None:
        with connected(replace(GITEA, host="git.example.com", url="https://git.example.com")), \
                self.assertRaisesRegex(ManifestError, "describes git.example.com as ssh"):
            self._git("git.example.com/org/app")

    def test_no_user_on_an_ssh_server(self) -> None:
        self.assertEqual(SSH.keys, resolve_forge("git.example.com", None, "dev").keys)
        with self.assertRaisesRegex(ManifestError, "keys, not users"):
            resolve_forge("git.example.com", None, "dev", "alice")

    def test_create_actions_are_refused(self) -> None:
        with self.assertRaisesRegex(ManifestError, "creating a repo needs a forge's API"):
            self._git(**{"git_gate": {**DECLARED["git_gate"],
                                      "actions": {"create": ["git.example.com/org/*"]}}})

    def test_trust_names_the_key_by_fingerprint(self) -> None:
        bottle = self._git("git.example.com/org/lib")
        self.assertIn("fetch   ssh://git@git.example.com:2222/org/lib.git "
                      "(this host's key SHA256:lib)", grants(bottle))
        self.assertEqual(["forge   git.example.com: SSH server at "
                          "ssh://git@git.example.com:2222, host key ssh-ed25519 SSH, "
                          "keys SHA256:lib"], forge_lines(bottle))
        self.assertNotIn("/keys/lib", "\n".join(grants(bottle) + forge_lines(bottle)))


class TestNoLongFormInFiles(unittest.TestCase):
    def test_the_parse_check_refuses_any_long_form(self) -> None:
        for key in ("{provider: static, path: /k}", "{provider: ssh, path: /k}",
                    "{provider: gitea, forge_token_env: T}", "{provider: nope}"):
            text = ("---\ngit_gate:\n  repos:\n    lib:\n"
                    f"      url: ssh://git@git.example.com/org/lib.git\n      key: {key}\n---\n")
            with self.subTest(key=key):
                why = problem("bottles/dev.md", text)
                self.assertIn("bottle 'dev' uses the old git-gate format", why)
        self.assertIn("isn't one bot-bottle knows", problem("bottles/dev.md", (
            "---\ngit_gate:\n  repos:\n    lib:\n      url: ssh://git@h/o/r.git\n"
            "      key: {provider: nope}\n---\n")))
        self.assertEqual("", problem("bottles/dev.md", (
            "---\ngit_gate:\n  forges:\n    git.example.com: {kind: ssh}\n"
            "  repos:\n    - git.example.com/org/lib:write\n---\n")))

    def test_a_launch_refuses_it_too(self) -> None:
        with self.assertRaises(OldGitGateFormatError) as ctx:
            resolve_bottles({"dev": {"git_gate": {"repos": {"lib": {
                "url": "ssh://git@git.example.com/org/lib.git",
                "key": {"provider": "ssh", "path": "/k"}}}}}})
        self.assertIn("- git.example.com/org/lib", str(ctx.exception))


class _Cli(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))
        serve_host_controller(self)
        as_cli(self)

    def run_cmd(self, *argv: str) -> str:
        out = io.StringIO()
        def said(message: str) -> None:
            out.write(message + "\n")
        with redirect_stdout(out), \
                mock.patch("bot_bottle.cli.commands.forge.info", side_effect=said), \
                mock.patch("bot_bottle.cli.commands.forge_key.info", side_effect=said):
            self.assertIn(cmd_forge(list(argv)), (0, None))
        return out.getvalue()

    def store(self) -> ForgeStore:
        return ForgeStore(host_db_path())


class TestConnectCommand(_Cli):
    def test_connect_pins_the_host_key(self) -> None:
        with mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                        return_value="ssh-ed25519 SCANNED") as scan:
            out = self.run_cmd("connect", "ssh://git@git.example.com:2222")
        scan.assert_called_once_with("git.example.com", "2222")
        self.assertIn("connected the SSH server git.example.com", out)
        self.assertIn("forge key add git.example.com", out)
        conn = self.store().connections()["git.example.com"]
        self.assertEqual(("ssh", "", "ssh://git@git.example.com:2222", "ssh-ed25519 SCANNED"),
                         (conn.kind, conn.url, conn.ssh, conn.host_key))
        self.assertIn("git.example.com              ssh     0 keys", self.run_cmd("list"))
        shown = self.run_cmd("show", "git.example.com")
        self.assertIn("git.example.com: SSH server", shown)

    def test_a_given_host_key_is_pinned_without_asking(self) -> None:
        with mock.patch("bot_bottle.git_gate.host_key.fetch_host_key") as scan:
            self.run_cmd("connect", "ssh://git@upstream.invalid", "--host-key", "ssh-ed25519 K")
        scan.assert_not_called()
        self.assertEqual("ssh-ed25519 K",
                         self.store().connections()["upstream.invalid"].host_key)

    def test_refused(self) -> None:
        for url, why in (("ssh://git@h.example.com/org/r.git", "no path"),
                         ("ssh://h.example.com", "ssh://user@host")):
            with self.subTest(url=url), self.assertRaisesRegex(ConnectError, why):
                connect_ssh(url)
        with self.assertRaisesRegex(ConnectError, "<type> <base64>"):
            connect_ssh("ssh://git@h.example.com", "nonsense")
        with mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                        side_effect=RuntimeError("no route")), \
                self.assertRaisesRegex(ConnectError, "no route"):
            connect_ssh("ssh://git@h.example.com")
        with self.assertRaisesRegex(ConnectError, "forge key add"):
            connect("ssh://git@h.example.com", "token")
        self.store().add_user(GITEA, GITEA.users[0])
        with self.assertRaisesRegex(ConnectError, "connected as a gitea forge"):
            connect_ssh(f"ssh://git@{GITEA.host}", "ssh-ed25519 K")


@unittest.skipUnless(HAS_KEYGEN, "needs ssh-keygen")
class TestKeyCommand(_Cli):
    def setUp(self) -> None:
        super().setUp()
        self.store().connect_server(replace(SSH, keys=()))
        self.key = self.root / "deploy"
        self.fp = _keygen(self.key)

    def test_add_list_remove(self) -> None:
        out = self.run_cmd("key", "add", "git.example.com", "org/lib", str(self.key), "--write")
        self.assertIn(f"uses {self.key} ({self.fp}) for org/lib, read and write", out)
        os.chdir(self.root)
        self.addCleanup(os.chdir, "/")
        self.run_cmd("key", "add", "git.example.com", "--default", "deploy")
        conn = self.store().connections()["git.example.com"]
        self.assertEqual({ForgeKey("org/lib", str(self.key), self.fp, True),
                          ForgeKey("", str(self.key), self.fp, False)}, set(conn.keys))
        listed = self.run_cmd("key", "list")
        self.assertIn("org/lib", listed)
        self.assertIn("(default)", listed)
        self.assertIn(" ok", listed)
        self.assertIn("2 keys", self.run_cmd("list"))
        self.assertIn("key       org/lib", self.run_cmd("show", "git.example.com"))
        out = self.run_cmd("key", "remove", "git.example.com", "--default")
        self.assertIn("the file stays", out)
        self.assertTrue(self.key.exists())
        self.assertEqual(1, len(self.store().connections()["git.example.com"].keys))

    def test_two_forges_may_share_a_key(self) -> None:
        self.store().connect_server(replace(SSH, host="other.example.com", keys=(),
                                            ssh="ssh://git@other.example.com"))
        self.run_cmd("key", "add", "other.example.com", "--default", str(self.key))
        out = self.run_cmd("key", "add", "git.example.com", "--default", str(self.key))
        self.assertIn("the same key is other.example.com's too", out)
        self.run_cmd("key", "remove", "other.example.com", "--default")
        self.assertEqual(1, len(self.store().connections()["git.example.com"].keys))

    def test_list_shows_a_changed_or_missing_key(self) -> None:
        self.run_cmd("key", "add", "git.example.com", "org/lib", str(self.key))
        self.key.unlink()
        Path(f"{self.key}.pub").unlink()
        self.assertIn("can't be used", self.run_cmd("key", "list", "git.example.com"))
        _keygen(self.key)
        self.assertIn("changed: now", self.run_cmd("key", "list"))

    def test_refused(self) -> None:
        self.key.chmod(0o644)
        for argv, why in ((("git.example.com", "org/lib", str(self.key)), "readable by others"),
                          (("nowhere.example.com", "org/lib", str(self.key)), "isn't an SSH"),
                          (("git.example.com", "lib", str(self.key)), "isn't <owner>/<repo>"),
                          (("git.example.com", str(self.key)), "usage"),
                          (("git.example.com", "--default", "org/lib", str(self.key)), "usage")):
            with self.subTest(argv=argv), self.assertRaises(Die), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.run_cmd("key", "add", *argv)
            self.assertIn(why, err.getvalue())
        with self.assertRaises(Die):
            self.run_cmd("key", "remove", "git.example.com", "org/lib")
        with self.assertRaises(Die):
            self.run_cmd("key", "list", "nowhere.example.com")


class TestHostStateRoutes(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))

    def test_routes(self) -> None:
        bare = replace(SSH, keys=())
        host_state.forge_connect({"forge": bare.to_json()})
        reply = host_state.forge_key({"host": SSH.host, "key": SSH.keys[0].to_json()})
        self.assertEqual(1, len(reply["forge"]["keys"]))  # type: ignore[index]
        self.assertEqual({"changed": 1}, host_state.forge_key(
            {"path": "/keys/default", "fingerprint": "SHA256:n"}))
        self.assertEqual("/keys/default", host_state.forge_key_remove(
            {"host": SSH.host, "repo": ""})["key"]["path"])  # type: ignore[index]
        cases: list[tuple[Any, dict[str, object]]] = [
            (host_state.forge_key, {"host": SSH.host, "key": {}}),
            (host_state.forge_key, {"key": {}}),
            (host_state.forge_key, {"path": 1, "fingerprint": "x"}),
            (host_state.forge_key_remove, {"host": SSH.host, "repo": "o/r"}),
            (host_state.forge_key_remove, {"host": 1})]
        for route, body in cases:
            with self.subTest(body=body), self.assertRaises(ValueError):
                route(body)


class TestConsole(unittest.TestCase):
    def test_status_reports_keys_without_paths(self) -> None:
        with mock.patch("bot_bottle.forge_connections.connections",
                        return_value={SSH.host: SSH}), \
                mock.patch("bot_bottle.forge_keys.check_file",
                           side_effect=["SHA256:dflt", "SHA256:moved"]):
            (status,) = forge_link.status()
        self.assertEqual({"host": "git.example.com", "kind": "ssh", "url": "",
                          "ssh": "ssh://git@git.example.com:2222", "users": [], "keys": [
                              {"repo": None, "default": True, "write": True,
                               "fingerprint": "SHA256:dflt", "state": "ok", "message": ""},
                              {"repo": "org/lib", "default": False, "write": False,
                               "fingerprint": "SHA256:moved", "state": "changed",
                               "message": "the key file changed since it was added (was "
                                          "SHA256:lib); the next launch uses it"}]}, status)

    def test_a_missing_or_unusable_key_says_so_without_its_path(self) -> None:
        with mock.patch("bot_bottle.forge_connections.connections",
                        return_value={SSH.host: SSH}), \
                mock.patch("bot_bottle.forge_keys.check_file", side_effect=[
                    forge_keys.KeyFileError("/keys/default isn't there"),
                    forge_keys.KeyFileError("/keys/lib is readable by others")]):
            (status,) = forge_link.status()
        self.assertEqual(["missing", "unusable"], [k["state"] for k in status["keys"]])
        self.assertNotIn("/keys", repr(status))

    def test_repos_are_the_keys(self) -> None:
        with connected(SSH):
            reply = asyncio.run(forge_link.forge_repos({"forge": "git.example.com",
                                                        "request_id": "r"}))
        self.assertEqual({"type": "forge_repos", "request_id": "r", "forge": "git.example.com",
                          "login": "", "truncated": False,
                          "repos": [{"full_name": "org/lib", "private": None, "installed": None,
                                     "permissions": {"pull": True, "push": False,
                                                     "admin": False}}],
                          "default_key": {"write": True, "fingerprint": "SHA256:dflt"}}, reply)
        with connected(replace(SSH, keys=SSH.keys[1:])):
            self.assertIsNone(forge_link.repos("git.example.com")["default_key"])


class TestNoApi(unittest.TestCase):
    def test_create_repo_is_refused(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.hostctl import repo_create
        with connected(SSH), mock.patch("bot_bottle.bottle_state.read_metadata",
                                        return_value=None):
            with self.assertRaisesRegex(repo_create.CreateRepoError, "is an SSH server"):
                repo_create.plan("s", "git.example.com/org/new")

    def test_a_forge_connect_card_for_an_ssh_server(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.hostctl import remote_actions
        request = mock.Mock(proposed="ssh://git@git.example.com", justification="")
        with connected(SSH):
            lines, options = remote_actions.forge_attention(request, "ssh")
        self.assertEqual([], options)
        self.assertIn("kind: SSH server", lines)


@unittest.skipUnless(HAS_KEYGEN, "needs ssh-keygen")
class TestLiveGrant(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))
        self.key = self.root / "k"
        self.fp = _keygen(self.key)

    def test_an_ssh_grant_adds_the_hosts_key_with_nothing_minted(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.git_gate.live_grant import GrantError, grant_entry, provision_live
        from bot_bottle.git_gate.provision import saved_upstreams
        from bot_bottle.secret_source import MappingSecrets
        conn = replace(SSH, keys=(ForgeKey("", str(self.key), self.fp, write=True),))
        with connected(conn):
            forge = resolve_forge("git.example.com", None, "")
        entry = grant_entry("git.example.com/org/app:write", forge)
        stage = self.root / "stage"
        with mock.patch("bot_bottle.git_gate.live_grant._provision_dynamic_key") as mint:
            upstream = provision_live(entry, slug="s-1", stage_dir=stage,
                                      secrets=MappingSecrets({}), grant="g")
        mint.assert_not_called()
        self.assertEqual((str(self.key), True), (upstream.identity_file, upstream.writable))
        self.assertTrue(upstream.known_hosts_file.is_file())
        self.assertEqual(["app"], [u.name for u in saved_upstreams("s-1")])
        gone = grant_entry("git.example.com/org/gone",
                           replace(forge, keys=(ForgeKey("", str(self.root / "x"), "f"),)))
        with self.assertRaisesRegex(GrantError, "can't be used"):
            provision_live(gone, slug="s-1", stage_dir=stage, secrets=MappingSecrets({}))
        with self.assertRaises(MissingKeyError):
            grant_entry("git.example.com/org/app", replace(forge, keys=()))


class TestReview(unittest.TestCase):
    def test_a_proposals_ssh_server_shows_its_keys(self) -> None:
        # pylint: disable-next=import-outside-toplevel,protected-access
        from bot_bottle.suggest.review import _File, _forge_lines, _Repo
        files = [_File("dev", "", repos=[_Repo("org/lib", "git.example.com", True)])]
        with connected(SSH):
            (line,) = _forge_lines(files)
        self.assertEqual(("git.example.com", "SSH server, 2 keys here"), (line.what, line.detail))


class TestUsers(unittest.TestCase):
    def test_ssh_forge_users_are_refused(self) -> None:
        with self.assertRaises(ForgeConnectionError):
            replace(SSH, users=(ForgeUser("a", "T"),)).user("a")


if __name__ == "__main__":
    unittest.main()
