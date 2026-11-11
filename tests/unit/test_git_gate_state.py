"""Unit: a bottled agent's git-gate state is host DB rows (ADR 0020): its
upstreams, live grants and trailer, written only by the host controller and
removed with the bottled agent."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import bottle_state
from bot_bottle.control.git_gate_render import GitGateUpstream
from bot_bottle.control.hostctl import host_db
from bot_bottle.git_gate import state
from bot_bottle.git_gate.provision import (
    DeployKeyRef, forget_live_grant, live_grant_refs, record_live_grant, save_upstreams,
    saved_upstreams,
)
from bot_bottle.git_gate.service import load_trailer_settings, save_trailer_settings
from bot_bottle.lib import db_store
from bot_bottle.paths import host_db_path
from tests.unit import as_cli, bottled_agent, use_bottle_root


def _up(name: str, **kw: object) -> GitGateUpstream:
    return dataclasses.replace(GitGateUpstream(
        name=name, upstream_url=f"ssh://git@h/o/{name}.git", upstream_host="h",
        upstream_port="22", identity_file=f"/k/{name}-key", known_host_key=""), **kw)


def _ref(name: str, grant: str = "") -> DeployKeyRef:
    return DeployKeyRef(name, "GITEA_TOKEN", "", "h", f"o/{name}.git", grant=grant)


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))


class TestRows(_Root):
    def test_upstreams_round_trip_in_order(self) -> None:
        known_hosts = self.root / "app-known_hosts"
        known_hosts.write_text("h ssh-ed25519 K\n")
        app = _up("app", known_hosts_file=known_hosts, writable=False,
                  protect_default_branch=True)
        https = _up("design", transport="https", token_secret="GITHUB_COM_TOKEN",
                    identity_file="", upstream_port="443")
        save_upstreams("s1", (https, app))
        self.assertEqual((https, app), saved_upstreams("s1"))
        self.assertEqual((), saved_upstreams("s2"))
        # A known_hosts file that's gone isn't handed to the gateway.
        known_hosts.unlink()
        self.assertEqual(Path(), saved_upstreams("s1")[1].known_hosts_file)
        # Saving again replaces the whole table.
        save_upstreams("s1", (app,))
        self.assertEqual(["app"], [u.name for u in saved_upstreams("s1")])

    def test_one_upstream_is_replaced_last_or_dropped(self) -> None:
        save_upstreams("s1", (_up("a"), _up("b")))
        store = state.store()
        store.put_upstream("s1", _up("a", writable=False))
        self.assertEqual([("b", True), ("a", False)],
                         [(u.name, u.writable) for u in saved_upstreams("s1")])
        store.drop_upstream("s1", "b")
        self.assertEqual(["a"], [u.name for u in saved_upstreams("s1")])

    def test_a_regrant_replaces_the_record_and_goes_last(self) -> None:
        record_live_grant("s1", _ref("app"))
        record_live_grant("s1", _ref("lib"))
        record_live_grant("s1", _ref("app", grant="h/o/app:write"))
        self.assertEqual([_ref("lib"), _ref("app", grant="h/o/app:write")],
                         list(live_grant_refs("s1")))
        forget_live_grant("s1", "lib")
        self.assertEqual(["app"], [r.name for r in live_grant_refs("s1")])
        self.assertEqual((), live_grant_refs("s2"))

    def test_the_trailer(self) -> None:
        self.assertEqual(("", ""), load_trailer_settings("s1"))
        save_trailer_settings("s1", "dev/s1", "warn")
        save_trailer_settings("s1", "dev/s1", "require")
        self.assertEqual(("dev/s1", "require"), load_trailer_settings("s1"))

    def test_the_tables_are_the_host_dbs_and_machine_specific(self) -> None:
        stores = [s for s in host_db.stores(host_db_path()) if isinstance(s, state.GitGateStore)]
        self.assertEqual(1, len(stores))
        self.assertEqual({db_store.MACHINE_SPECIFIC},
                         set(state.GitGateStore.TABLES.values()))
        save_trailer_settings("s1", "dev/s1", "warn")
        self.assertEqual([], [t for t in host_db.migrate() if t.startswith("git_gate")])
        self.assertEqual(("dev/s1", "warn"), load_trailer_settings("s1"))


class TestOneTransaction(_Root):
    def test_recording_a_grant_reads_and_writes_under_one_write_lock(self) -> None:
        record_live_grant("s1", _ref("app"))
        begun: list[str] = []
        real = db_store.immediate

        def spy(conn):  # type: ignore[no-untyped-def]
            begun.append("BEGIN IMMEDIATE")
            return real(conn)

        with mock.patch.object(db_store, "immediate", spy):
            record_live_grant("s1", _ref("app", grant="h/o/app"))
        self.assertEqual(["BEGIN IMMEDIATE"], begun)

    def test_a_failed_regrant_leaves_the_old_record(self) -> None:
        record_live_grant("s1", _ref("app"))
        broken = {"name": "app", "forge_token_env": "T"}  # no other fields
        with self.assertRaises(KeyError):
            state.store().record_live_grant("s1", broken)
        self.assertEqual([_ref("app")], list(live_grant_refs("s1")))


class TestOnlyTheHostController(_Root):
    def test_the_cli_cant_open_them(self) -> None:
        as_cli(self)
        with self.assertRaisesRegex(RuntimeError, "only the host controller"):
            live_grant_refs("s1")
        with self.assertRaisesRegex(RuntimeError, "only the host controller"):
            save_trailer_settings("s1", "dev/s1", "warn")


class TestRemovedWithTheBottledAgent(_Root):
    def test_removing_it_deletes_its_rows_and_no_others(self) -> None:
        for identity in ("s1", "s2"):
            bottled_agent(identity)
            save_upstreams(identity, (_up("app"),))
            record_live_grant(identity, _ref("app"))
            save_trailer_settings(identity, f"dev/{identity}", "warn")
        self.assertTrue(bottle_state.remove_bottled_agent("s1"))
        self.assertEqual(((), (), ("", "")),
                         (saved_upstreams("s1"), live_grant_refs("s1"),
                          load_trailer_settings("s1")))
        self.assertEqual((1, 1, ("dev/s2", "warn")),
                         (len(saved_upstreams("s2")), len(live_grant_refs("s2")),
                          load_trailer_settings("s2")))


if __name__ == "__main__":
    unittest.main()
