"""Unit: a library conflict pending on the console (PRD 0131 chunk 4). The
host keeps its own version and keeps pushing; the conflict is kept in the
host DB, so a restart still knows it, and clears once a fetched `main`
contains the commit the host had. CLI launches and `bot-bottle status` say
it's pending. Real git for the sync; a local bare repo is the library app."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import remote as remote_cmd
from bot_bottle.control.hostctl import host_db, host_state, library_link as ll, pairing
from bot_bottle.control.hostctl.library_conflict import Conflict, LibraryConflictStore
from bot_bottle.manifest.versions import record
from bot_bottle.paths import host_db_path
from tests.unit import use_bottle_root
from tests.unit.test_library_link import _FakeSync, _git, _Library


class Store(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.path = Path(tmp) / "db" / "host.db"

    def test_kept_across_a_restart_with_when_it_was_first_reported(self) -> None:
        store = LibraryConflictStore(self.path)
        self.assertIsNone(store.pending())
        first = store.report(["bottles/dev.md"], "a" * 40)
        later = LibraryConflictStore(self.path).report(["bottles/dev.md", "bottles/ops.md"],
                                                       "b" * 40)
        self.assertEqual(first.since, later.since)
        self.assertEqual(Conflict(("bottles/dev.md", "bottles/ops.md"), first.since, "b" * 40),
                         LibraryConflictStore(self.path).pending())
        self.assertTrue(store.clear())
        self.assertFalse(store.clear())
        self.assertIsNone(store.pending())

    def test_unreadable_files_are_none(self) -> None:
        store = LibraryConflictStore(self.path)
        store.report([], "")
        with store._connection() as conn:  # pylint: disable=protected-access
            conn.execute("UPDATE library_conflict SET files = 'nope'")
        pending = store.pending()
        assert pending is not None
        self.assertEqual((), pending.files)
        self.assertEqual({"files": [], "since": pending.since, "tip": ""}, pending.to_json())

    def test_it_is_a_host_db_table(self) -> None:
        self.assertIn("library_conflict",
                      [t for s in host_db.stores(self.path) for t in s.TABLES])


class AcrossHosts(_Library):
    """Two hosts edit `dev` differently: delphi's branch conflicts."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(ll.log, "info"))
        self.delos = self.host("delos", {"bottles/dev.md": "base\n"})
        self.delos.sync()
        self.merge_as_console("delos")
        self.delphi = self.host("delphi", {"bottles/ci.md": "ci\n"})
        self.delphi.sync()
        self.merge_as_console("delphi")
        self.assertTrue(self.delphi.sync().moved)  # now both have both
        self.edit(self.delos, "delos\n")
        self.delos.sync()
        self.merge_as_console("delos")
        self.edit(self.delphi, "delphi\n")
        self.store = LibraryConflictStore(self.tmp / "db" / "host.db")
        self.sent: list[dict[str, Any]] = []
        self.link = ll.LibraryLink(self.send, make_sync=lambda: self.delphi,
                                   conflicts=self.store)

    async def send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    @staticmethod
    def edit(host: ll.LibrarySync, text: str) -> None:
        (host.root / "bottles/dev.md").write_text(text)
        record("edit", root=host.root)

    def conflict(self) -> None:
        """delphi pushes; the console's merge conflicts and says so."""
        self.assertTrue(self.link.sync_now().pushed)
        with self.assertRaises(subprocess.CalledProcessError):
            self.merge_as_console("delphi")
        with mock.patch.object(ll.log, "warn"):
            asyncio.run(self.link.handle({"type": "library_conflict",
                                          "files": ["bottles/dev.md"]}))

    def resolve_as_console(self, keep: str) -> None:
        """The console's resolution: `main` merges delphi's branch, keeping
        `keep`'s side."""
        work = self.tmp / f"resolve-{time.monotonic_ns()}"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(work)], check=True,
                       capture_output=True)
        _git(work, "checkout", "-q", "-B", "main", "origin/main")
        _git(work, "-c", "user.name=console", "-c", "user.email=c@c", "merge", "-q",
             "--no-edit", "-X", keep, "origin/hosts/delphi")
        _git(work, "push", "-q", "origin", "main")

    def test_pending_until_main_contains_the_hosts_commit(self) -> None:
        self.conflict()
        self.assertEqual(["bottles/dev.md"], self.link.conflict)
        pending = self.store.pending()
        assert pending is not None
        self.assertEqual(_git(self.delphi.root, "rev-parse", "HEAD"), pending.tip)
        # Main moving elsewhere doesn't resolve it, and delphi keeps its own.
        self.link.sync_now()
        self.assertEqual(["bottles/dev.md"], self.link.conflict)
        self.assertEqual("delphi\n", (self.delphi.root / "bottles/dev.md").read_text())
        self.resolve_as_console("theirs")
        self.assertTrue(self.link.sync_now().moved)
        self.assertEqual([], self.link.conflict)
        self.assertEqual("delphi\n", (self.delphi.root / "bottles/dev.md").read_text())

    def test_new_commits_are_pushed_while_it_waits(self) -> None:
        self.conflict()
        (self.delphi.root / "bottles/ops.md").write_text("ops\n")
        record("another", root=self.delphi.root)
        head = _git(self.delphi.root, "rev-parse", "HEAD")
        self.assertEqual(head, self.link.sync_now().pushed)
        self.assertEqual(head, _git(self.bare, "rev-parse", "refs/heads/hosts/delphi"))
        self.assertEqual(["bottles/dev.md"], self.link.conflict)
        # Resolved with the newer commit in it: cleared, main fast-forwards.
        self.resolve_as_console("ours")
        self.assertTrue(self.link.sync_now().moved)
        self.assertIsNone(self.store.pending())
        self.assertEqual("delos\n", (self.delphi.root / "bottles/dev.md").read_text())

    def test_resolved_at_the_reported_commit_while_a_newer_one_waits(self) -> None:
        self.conflict()
        self.resolve_as_console("theirs")
        self.edit(self.delphi, "newer\n")  # after the resolution: main lacks it
        synced = self.link.sync_now()
        self.assertTrue(synced.pushed)
        self.assertIsNone(self.store.pending())

    def test_turning_the_library_off_forgets_it(self) -> None:
        self.conflict()
        asyncio.run(self.link.handle({"type": "library_sync", "enabled": False}))
        self.assertIsNone(self.store.pending())


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))
        self.addCleanup(setattr, ll, "_active", None)

    @staticmethod
    def pending(*files: str, since: float | None = None) -> None:
        store = LibraryConflictStore(host_db_path())
        store.report(list(files), "tip")
        if since is not None:
            with store._connection() as conn:  # pylint: disable=protected-access
                conn.execute("UPDATE library_conflict SET since = ?", (since,))

    @staticmethod
    def pair(enabled: bool = True) -> None:
        pairing.store().update({"url": "https://console.example/", "access_token": "t",
                                "enabled": enabled}, new=True)

    def link(self, fake: _FakeSync | None = None) -> ll.LibraryLink:
        async def send(msg: dict[str, Any]) -> None:
            del msg
        made = fake or _FakeSync()
        link = ll.LibraryLink(send, make_sync=lambda: made)  # type: ignore[arg-type,return-value]
        link.enabled = True
        ll._active = link  # pylint: disable=protected-access
        return link


class Launching(_Root):
    def test_a_pending_conflict_names_its_bottles_and_this_host(self) -> None:
        self.link()
        self.pending("bottles/dev.md")
        with mock.patch.object(ll.socket, "gethostname", return_value="delphi.lan"):
            self.assertEqual("library: dev has a conflict waiting in the console; "
                             "launching delphi's version", ll.sync_for_launch())

    def test_several_files_and_none(self) -> None:
        host = ll.socket.gethostname().split(".")[0]
        conflict = Conflict(("bottles/dev.md", "agents/x.md", "README"), 0.0, "")
        self.assertIn("dev, agent x and README have a conflict", ll.conflict_note(conflict))
        self.assertEqual("library: this host's library changes have a conflict waiting in "
                         f"the console; launching {host}'s version",
                         ll.conflict_note(Conflict((), 0.0, "")))

    def test_after_a_restart_before_the_link_connects(self) -> None:
        self.pending("bottles/dev.md")
        self.assertEqual("", ll.sync_for_launch())  # not paired: nothing to resolve
        self.pair()
        self.assertIn("dev has a conflict", ll.sync_for_launch())
        self.pair(enabled=False)
        self.assertEqual("", ll.sync_for_launch())

    def test_none_pending_is_the_sync_note(self) -> None:
        self.link(_FakeSync(error=ll.LibraryError("refused")))
        self.assertIn("couldn't sync", ll.sync_for_launch())

    def test_an_unreadable_host_db_is_none(self) -> None:
        with mock.patch.object(ll, "_conflicts", side_effect=OSError("gone")):
            self.link()
            self.assertIsNone(ll.pending_conflict())


class Status(_Root):
    def test_off(self) -> None:
        self.assertEqual({"state": "off"}, ll.report())
        self.assertEqual("library: off", remote_cmd.library_line(ll.report()))

    def test_in_sync_and_syncing(self) -> None:
        link = self.link()
        with mock.patch.object(_FakeSync, "heads", return_value=("a" * 40, "a" * 40)):
            report = ll.report()
        self.assertEqual("library: in sync at aaaaaaa", remote_cmd.library_line(report))
        with mock.patch.object(_FakeSync, "heads", return_value=("a" * 40, "b" * 40)):
            report = ll.report()
        self.assertEqual("library: at aaaaaaa, syncing with the console "
                         "(its main is at bbbbbbb)", remote_cmd.library_line(report))
        with mock.patch.object(link, "syncer", side_effect=pairing.RemoteError("unpaired")):
            report = ll.report()
        self.assertEqual("library: at no commits yet, syncing with the console",
                         remote_cmd.library_line(report))

    def test_a_conflict_and_how_long_ago(self) -> None:
        self.link()
        self.pending("bottles/dev.md", since=time.time() - 2 * 3600 - 5)
        self.assertEqual("library: conflict pending in the console (bottles/dev.md) "
                         "since 2h ago", remote_cmd.library_line(ll.report()))
        self.assertTrue(remote_cmd.library_line(
            {"state": "conflict", "files": [], "since": time.time() - 3 * 86400}
        ).endswith("(files unknown) since 3d ago"))
        for seconds, said in ((5, "5s ago"), (120, "2m ago")):
            self.assertEqual(said, remote_cmd._ago(seconds))  # pylint: disable=protected-access

    def test_the_host_route_reports_it_and_status_prints_it(self) -> None:
        self.link()
        self.pending("bottles/dev.md")
        reply = host_state.get_pairing({})
        link = reply["link"]
        assert isinstance(link, dict)
        self.assertEqual("conflict", link["library"]["state"])
        with mock.patch.object(remote_cmd, "running_pairing",
                               return_value=(pairing.Pairing(), dict(link))):
            lines = remote_cmd.link_line().splitlines()
        self.assertTrue(lines[0].startswith("console link: off"))
        self.assertTrue(lines[1].startswith("library: conflict pending in the console"))

    def test_the_real_heads(self) -> None:
        sync = ll.LibrarySync(host_id="h", root=self.root / "nowhere")
        self.assertEqual(("", ""), sync.heads())
        repo = self.root / "repo"
        (repo / "bottles").mkdir(parents=True)
        (repo / "bottles/dev.md").write_text("x\n")
        record("one", root=repo)
        sync = ll.LibrarySync(host_id="h", root=repo)
        self.assertEqual((_git(repo, "rev-parse", "HEAD"), ""), sync.heads())


if __name__ == "__main__":
    unittest.main()
