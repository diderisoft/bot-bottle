"""The forges a host connected: `forge_connections` and `forge_users` in the
host DB (ADR 0019, ADR 0020)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bot_bottle import forge_connections
from bot_bottle.forge_connections import (
    ForgeConnection, ForgeConnectionError, ForgeStore, ForgeUser, host_of, secret_name,
)
from bot_bottle.lib.db_store import PORTABLE
from bot_bottle.paths import host_db_path
from bot_bottle.secret_source import FileSecrets, write_secret
from tests.unit import as_cli, serve_host_controller, use_bottle_root

GITEA = ForgeConnection(host="gitea.example.com", kind="gitea", url="https://gitea.example.com",
                        ssh="ssh://git@10.0.4.2:30009", host_key="ssh-ed25519 AAAA")
ALICE = ForgeUser("alice", "GITEA_EXAMPLE_COM_TOKEN")
CODEX = ForgeUser("codex", "CODEX_GITEA_EXAMPLE_COM_TOKEN")


def _json(**user: object) -> dict[str, object]:
    return {"host": "gitea.example.com", "kind": "gitea", "url": "https://gitea.example.com",
            "users": [{"name": "a", "token": "secret:T", **user}]}


class TestJson(unittest.TestCase):
    def test_round_trip_and_defaults(self) -> None:
        conn = ForgeConnection.from_json(_json(default=True))
        self.assertEqual("ssh://git@gitea.example.com", conn.ssh)  # the default
        self.assertEqual(conn, ForgeConnection.from_json(conn.to_json()))
        self.assertEqual("secret:T", conn.to_json()["users"][0]["token"])  # type: ignore[index]
        self.assertEqual({"kind": "gitea", "url": "https://gitea.example.com",
                          "ssh": "ssh://git@gitea.example.com", "host_key": "",
                          "user": "a", "transport": ""}, conn.snapshot(conn.user()))
        with self.assertRaisesRegex(ForgeConnectionError, "nobody isn't connected"):
            conn.user("nobody")

    def test_a_token_names_a_secret(self) -> None:
        for bad in ("$T", "secret:", "secret:a b", "plain", "?"):
            with self.subTest(bad=bad), \
                    self.assertRaisesRegex(ForgeConnectionError, "as secret:NAME"):
                ForgeConnection.from_json(_json(token=bad))

    def test_refusals(self) -> None:
        cases = {
            "must be on": {**_json(), "url": "https://other.example.com"},
            "kind must": {**_json(), "kind": "gitlab"},
            "keyed by its host": {**_json(), "host": "a b"},
            "transport must be deploy-key": _json(transport="https"),
            "app is for GitHub": _json(app="x"),
            "default must be": _json(default="yes"),
            "only one user": {**_json(), "users": [
                {"name": "a", "token": "secret:T", "default": True},
                {"name": "b", "token": "secret:U", "default": True}]},
            "connected once": {**_json(), "users": [
                {"name": "a", "token": "secret:T"}, {"name": "a", "token": "secret:U"}]},
        }
        for fragment, body in cases.items():
            with self.subTest(fragment=fragment), \
                    self.assertRaisesRegex(ForgeConnectionError, fragment):
                ForgeConnection.from_json(body)

    def test_no_default_among_several(self) -> None:
        conn = ForgeConnection.from_json({**_json(), "users": [
            {"name": "a", "token": "secret:T"}, {"name": "b", "token": "secret:U"}]})
        with self.assertRaisesRegex(ForgeConnectionError, "none is the default"):
            conn.user()

    def test_names(self) -> None:
        self.assertEqual("GITEA_EXAMPLE_COM_TOKEN",
                         secret_name("gitea.example.com", "bot", first=True))
        self.assertEqual("CODEX_10_0_0_1_3000_TOKEN", secret_name("10.0.0.1:3000", "codex",
                                                                 first=False))
        self.assertEqual("g.example.com:3000", host_of("http://g.example.com:3000/x"))


class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))
        self.store = ForgeStore(host_db_path())

    def test_none_connected(self) -> None:
        self.assertEqual({}, self.store.connections())
        self.assertEqual({}, forge_connections.connections())

    def test_portable_tables(self) -> None:
        self.assertEqual({"forge_connections": PORTABLE, "forge_users": PORTABLE},
                         dict(ForgeStore.TABLES))

    def test_add_user_and_the_default(self) -> None:
        first = self.store.add_user(GITEA, ALICE)
        self.assertTrue(first.users[0].default)
        second = self.store.add_user(GITEA, CODEX)
        self.assertEqual([("alice", True), ("codex", False)],
                         [(u.name, u.default) for u in second.users])
        again = self.store.add_user(GITEA, ALICE)
        self.assertEqual({"alice"}, {u.name for u in again.users if u.default})
        made = self.store.add_user(GITEA, CODEX, make_default=True)
        self.assertEqual({"codex"}, {u.name for u in made.users if u.default})
        self.assertEqual(made, self.store.connections()["gitea.example.com"])
        moved = self.store.add_user(replace(GITEA, ssh="ssh://git@h"), ALICE)
        self.assertEqual("ssh://git@h", moved.ssh)

    def test_tokens_are_named_not_kept(self) -> None:
        self.store.add_user(GITEA, ALICE)
        with sqlite3.connect(host_db_path()) as db:
            self.assertEqual([("secret:GITEA_EXAMPLE_COM_TOKEN",)],
                             db.execute("SELECT token FROM forge_users").fetchall())

    def test_a_bad_connection_isnt_recorded(self) -> None:
        with self.assertRaisesRegex(ForgeConnectionError, "app is for GitHub"):
            self.store.add_user(GITEA, ForgeUser("a", "T", app="x"))
        self.assertEqual({}, self.store.connections())

    def test_remove_a_user_then_the_forge_and_their_tokens(self) -> None:
        secrets = self.root / "secrets"
        for name in ("GITEA_EXAMPLE_COM_TOKEN", "CODEX_GITEA_EXAMPLE_COM_TOKEN", "OTHER"):
            write_secret(secrets, name, "v")
        self.store.add_user(GITEA, ALICE)
        self.store.add_user(GITEA, CODEX)
        self.assertEqual(["codex"], forge_connections.remove("gitea.example.com", "codex"))
        self.assertEqual(["alice"], [u.name for u in
                                     self.store.connections()["gitea.example.com"].users])
        self.assertIsNone(FileSecrets(secrets).get("CODEX_GITEA_EXAMPLE_COM_TOKEN"))
        self.assertEqual(["alice"], forge_connections.remove("gitea.example.com"))
        self.assertEqual({}, self.store.connections())
        self.assertEqual("v", FileSecrets(secrets).get("OTHER"))
        with self.assertRaisesRegex(ForgeConnectionError, "isn't connected"):
            forge_connections.remove("gitea.example.com")


class TestThroughTheHostController(unittest.TestCase):
    """The CLI's side: the same calls, asked of the host controller."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))
        serve_host_controller(self)
        as_cli(self)

    def test_connect_list_and_remove(self) -> None:
        self.assertEqual({}, forge_connections.connections())
        added = forge_connections.add_user(GITEA, ALICE)
        self.assertEqual("alice", added.user().name)
        self.assertEqual(added, forge_connections.add_user(GITEA, ALICE))
        self.assertEqual({"gitea.example.com": added}, forge_connections.connections())
        self.assertEqual(added, ForgeStore(host_db_path()).connections()["gitea.example.com"])
        self.assertEqual(["alice"], forge_connections.remove("gitea.example.com"))
        self.assertIsNone(forge_connections.get("gitea.example.com"))

    def test_refusals_say_why(self) -> None:
        with self.assertRaisesRegex(ForgeConnectionError, "isn't connected"):
            forge_connections.remove("gitea.example.com")
        with self.assertRaisesRegex(ForgeConnectionError, "app is for GitHub"):
            forge_connections.add_user(GITEA, ForgeUser("a", "T", app="x"))


if __name__ == "__main__":
    unittest.main()
