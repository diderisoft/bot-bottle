"""Hand edits to the library (PRD 0131 chunk 5): committed once quiet,
authored as the host, and held back from sync while they don't parse. Real
git repos throughout."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import remote as remote_cmd
from bot_bottle.control.hostctl import (host_db, host_state, library_git as lg,
                                        library_link as ll, pairing)
from bot_bottle.control.hostctl.library_conflict import LibraryConflictStore
from bot_bottle.control.hostctl.library_held import (HeldFile, LibraryHeldStore, MAX_ERROR,
                                                     unparseable)
from bot_bottle.control.hostctl.library_watch import QUIET_SECONDS, HandEditWatch
from bot_bottle.manifest import parse_check
from bot_bottle.manifest.versions import (AUTHOR_KEY, VersionError, pending_edits, record,
                                          set_author)
from bot_bottle.paths import host_db_path
from tests.unit import use_bottle_root
from tests.unit.test_library_link import _FakeSync, _git, _Library

GOOD = "---\negress:\n  routes:\n    - host: example.com\n---\nA bottle.\n"
BAD = "---\negress: [\n---\n"


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Repo(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        (self.root / "bottles").mkdir()
        (self.root / "bottles/dev.md").write_text(GOOD)
        record("setup", root=self.root)

    def log(self, fmt: str = "%s") -> list[str]:
        return _git(self.root, "log", f"--format={fmt}").splitlines()

    def save(self, rel: str, text: str, *, at: float | None = None) -> None:
        path = self.root / rel
        path.write_text(text)
        stamp = time.time() if at is None else at
        os.utime(path, (stamp, stamp))


class QuietPeriod(_Repo):
    def setUp(self) -> None:
        super().setUp()
        self.clock = _Clock()
        self.watch = HandEditWatch(self.root, self.clock)

    def test_an_edit_is_committed_once_quiet(self) -> None:
        self.save("bottles/dev.md", GOOD + "more\n")
        self.assertFalse(self.watch.poll())  # first seen
        self.clock.now += QUIET_SECONDS / 2
        self.assertFalse(self.watch.poll())
        self.clock.now += QUIET_SECONDS / 2
        self.assertTrue(self.watch.poll())
        self.assertEqual(["edited by hand", "setup"], self.log())
        self.assertEqual([], pending_edits(root=self.root))
        self.clock.now += 10
        self.assertFalse(self.watch.poll())  # nothing left

    def test_a_burst_is_one_commit(self) -> None:
        for i in range(4):  # a save each second, as an editor's autosave might
            self.save("bottles/dev.md", GOOD + f"v{i}\n", at=1000.0 + i)
            self.assertFalse(self.watch.poll())
            self.clock.now += 1.0
        self.save("bottles/new.md", GOOD)
        self.assertFalse(self.watch.poll())
        self.clock.now += QUIET_SECONDS
        self.assertTrue(self.watch.poll())
        self.assertEqual(["edited by hand", "setup"], self.log())
        self.assertEqual(GOOD + "v3\n", _git(self.root, "show", "HEAD:bottles/dev.md") + "\n")

    def test_saving_the_same_text_again_waits_again(self) -> None:
        self.save("bottles/dev.md", GOOD + "x\n", at=1000.0)
        self.watch.poll()
        self.clock.now += QUIET_SECONDS - 0.5
        self.save("bottles/dev.md", GOOD + "x\n", at=1001.0)  # same status, new mtime
        self.assertFalse(self.watch.poll())
        self.clock.now += QUIET_SECONDS - 0.5
        self.assertFalse(self.watch.poll())
        self.clock.now += 0.5
        self.assertTrue(self.watch.poll())

    def test_a_deletion_is_an_edit(self) -> None:
        (self.root / "bottles/dev.md").unlink()
        self.watch.poll()
        self.clock.now += QUIET_SECONDS
        self.assertTrue(self.watch.poll())
        self.assertEqual("", _git(self.root, "ls-files", "bottles"))

    def test_untouched_never_commits(self) -> None:
        for _ in range(3):
            self.assertFalse(self.watch.poll())
            self.clock.now += QUIET_SECONDS
        self.assertEqual(["setup"], self.log())

    def test_a_refused_commit_waits_for_the_next_change(self) -> None:
        self.save("bottles/dev.md", GOOD + "y\n")
        self.watch.poll()
        self.clock.now += QUIET_SECONDS
        with mock.patch("bot_bottle.control.hostctl.library_watch.record_hand_edits",
                        side_effect=VersionError("holds a secret")) as rec:
            with self.assertRaises(VersionError):
                self.watch.poll()
            self.clock.now += 60
            self.assertFalse(self.watch.poll())
            self.assertEqual(1, rec.call_count)
        self.save("bottles/dev.md", GOOD + "z\n")
        self.watch.poll()
        self.clock.now += QUIET_SECONDS
        self.assertTrue(self.watch.poll())

    def test_no_repo_no_edits(self) -> None:
        self.assertEqual([], pending_edits(root=self.root / "nowhere"))


class Authorship(_Repo):
    def commit(self, text: str) -> str:
        (self.root / "bottles/dev.md").write_text(text)
        record("edit", root=self.root)
        return self.log("%an|%cn")[0]

    def test_unpaired_is_bot_bottle(self) -> None:
        self.assertEqual("bot-bottle|bot-bottle", self.commit("a\n"))

    def test_named_commits_carry_the_hosts_label_until_unnamed(self) -> None:
        set_author("delphi", root=self.root)
        self.assertEqual("delphi|bot-bottle", self.commit("a\n"))
        self.assertEqual("delphi", _git(self.root, "config", "--get", AUTHOR_KEY))
        set_author("", root=self.root)
        self.assertEqual("bot-bottle|bot-bottle", self.commit("b\n"))
        set_author("", root=self.root)  # unsetting twice is fine


class LinkAuthorship(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))
        self.addCleanup(setattr, ll, "_active", None)
        self.enterContext(mock.patch.object(ll.log, "info"))
        (self.root / "bottles").mkdir()

        async def send(msg: dict[str, Any]) -> None:
            del msg
        self.fake = _FakeSync()
        self.link = ll.LibraryLink(send, make_sync=lambda: self.fake,  # type: ignore[arg-type,return-value]
                                   root=self.root,
                                   conflicts=LibraryConflictStore(host_db_path()),
                                   held=LibraryHeldStore(host_db_path()))

    def author(self) -> str:
        (self.root / "bottles/x.md").write_text(str(time.monotonic_ns()))
        record("edit", root=self.root)
        return _git(self.root, "log", "-1", "--format=%an")

    async def test_on_a_paired_host_commits_carry_its_label(self) -> None:
        pairing.store().update({"url": "https://c", "access_token": "t", "label": "delphi"},
                               new=True)
        record("init", root=self.root)
        await self.link.handle({"type": "library_sync", "enabled": True})
        self.assertEqual("delphi", self.author())
        await self.link.handle({"type": "library_sync", "enabled": False})
        self.assertEqual("bot-bottle", self.author())

    async def test_a_repo_made_later_is_named_on_the_next_tick(self) -> None:
        pairing.store().update({"url": "https://c", "access_token": "t", "label": "delos"},
                               new=True)
        await self.link.handle({"type": "library_sync", "enabled": True})
        self.assertFalse((self.root / ".git").exists())
        record("init", root=self.root)
        await self.link.tick()
        self.assertEqual("delos", self.author())

    async def test_an_older_pairing_is_named_by_hostname(self) -> None:
        pairing.store().update({"url": "https://c", "access_token": "t"}, new=True)
        with mock.patch.object(ll.socket, "gethostname", return_value="delphi.lan"):
            self.assertEqual("delphi", ll.label())

    async def test_a_failure_to_name_it_is_logged(self) -> None:
        record("init", root=self.root)
        with mock.patch.object(ll, "set_author", side_effect=VersionError("locked")), \
                mock.patch.object(ll.log, "warn") as warn:
            await self.link.handle({"type": "library_sync", "enabled": True})
        self.assertIn("couldn't set", warn.call_args.args[0])
        self.assertFalse(self.link._authored)

    async def test_a_tick_commits_hand_edits_once_quiet_and_syncs(self) -> None:
        record("init", root=self.root)
        self.link.enabled = True
        clock = _Clock()
        self.link._watch = HandEditWatch(self.root, clock)
        self.link._seen = ll.head_stamp(self.root)
        (self.root / "bottles/dev.md").write_text(GOOD)
        await self.link.tick()
        clock.now += QUIET_SECONDS
        await self.link.tick()
        task = self.link._task
        assert task is not None
        await task
        self.assertEqual("edited by hand", _git(self.root, "log", "-1", "--format=%s"))
        self.assertEqual(1, self.fake.calls)

    async def test_a_refused_hand_edit_is_logged(self) -> None:
        self.link.enabled = True
        watch = mock.Mock()
        watch.poll.side_effect = VersionError("holds a secret")
        self.link._watch = watch
        with mock.patch.object(ll.log, "warn") as warn:
            await self.link.tick()
        self.assertIn("holds a secret", warn.call_args.args[0])


class PairedAs(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "host.db"  # pylint: disable=consider-using-with

    def test_the_label_is_kept_and_an_older_db_gains_it(self) -> None:
        with sqlite3.connect(self.path) as conn:  # the table before this chunk
            conn.execute("CREATE TABLE schema_versions (module TEXT PRIMARY KEY, "
                         "version INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO schema_versions VALUES ('pairing', 2)")
            conn.execute("CREATE TABLE pairing (id INTEGER PRIMARY KEY CHECK (id = 1), "
                         "url TEXT NOT NULL DEFAULT '', host_id TEXT NOT NULL DEFAULT '', "
                         "console_node_id TEXT NOT NULL DEFAULT '', console_ticket TEXT NOT "
                         "NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0, "
                         "access_token_secret TEXT NOT NULL DEFAULT '', refresh_token_secret "
                         "TEXT NOT NULL DEFAULT '')")
            conn.execute("CREATE TABLE console_link (id INTEGER PRIMARY KEY, updated REAL)")
            conn.execute("INSERT INTO pairing (id, url) VALUES (1, 'https://c')")
        store = pairing.PairingStore(self.path)
        self.assertEqual(("https://c", ""), (store.pairing().url, store.pairing().label))
        store.update({"label": "delphi"})
        self.assertEqual("delphi", pairing.PairingStore(self.path).pairing().label)

    def test_pairing_keeps_the_label_it_paired_as(self) -> None:
        from bot_bottle.cli.commands import login  # pylint: disable=import-outside-toplevel
        with mock.patch.object(login, "set_pairing") as set_pairing:
            login._save_credentials("http://c", {"host_id": "h"}, "delphi")
            login._save_credentials("http://c", {"host_id": "h"})
        self.assertEqual("delphi", set_pairing.call_args_list[0].kwargs["label"])
        self.assertNotIn("label", set_pairing.call_args_list[1].kwargs)


class ParseCheck(unittest.TestCase):
    def test_what_is_checked(self) -> None:
        self.assertTrue(parse_check.checked("bottles/dev.md"))
        self.assertTrue(parse_check.checked("agents/x.md"))
        for rel in ("bottles/notes.txt", "bottles/sub/dev.md", "README.md", ".gitignore", ""):
            self.assertFalse(parse_check.checked(rel), rel)

    def test_bottles(self) -> None:
        self.assertEqual("", parse_check.problem("bottles/dev.md", GOOD))
        self.assertEqual("", parse_check.problem(
            "bottles/ci.md", "---\nextends: [basic]\ngit_gate:\n  repos: "
            "[github.com/acme/app:write]\n  forges:\n    gitea.example.com: { kind: gitea }\n"
            "---\n"))
        self.assertIn("unterminated", parse_check.problem("bottles/dev.md", BAD))
        self.assertIn("unknown frontmatter key", parse_check.problem(
            "bottles/dev.md", "---\nnope: 1\n---\n"))
        self.assertIn("must be a string", parse_check.problem(
            "bottles/dev.md", "---\nenv:\n  A: 3\n---\n"))
        self.assertIn("extends", parse_check.problem("bottles/dev.md", "---\nextends: 3\n---\n"))
        self.assertIn("filename", parse_check.problem("bottles/Dev.md", GOOD))

    def test_agents(self) -> None:
        self.assertEqual("", parse_check.problem("agents/x.md", "---\nskills: []\n---\nHi.\n"))
        self.assertIn("skills", parse_check.problem("agents/x.md", "---\nskills: 3\n---\n"))
        self.assertIn("unknown frontmatter key", parse_check.problem(
            "agents/x.md", "---\nnope: 1\n---\n"))

    def test_names(self) -> None:
        self.assertEqual("dev", parse_check.names(["bottles/dev.md"]))
        self.assertEqual("dev, agent x and README",
                         parse_check.names(("bottles/dev.md", "agents/x.md", "README")))

    def test_unparseable_reads_each_once_and_shortens_errors(self) -> None:
        texts = {"bottles/dev.md": "---\n" + "k" * 1000 + ": 1\n---\n",
                 "bottles/ok.md": GOOD, "agents/gone.md": None, "bottles/a.txt": BAD}
        held = unparseable(["bottles/dev.md", "bottles/dev.md", "bottles/ok.md",
                            "agents/gone.md", "bottles/a.txt", ""], texts.get)
        self.assertEqual(["bottles/dev.md"], [f.path for f in held])
        self.assertEqual(MAX_ERROR, len(held[0].error))
        self.assertTrue(held[0].error.endswith("…"))


class HeldBack(_Library):
    """A hand edit that doesn't parse is committed but not pushed."""

    def setUp(self) -> None:
        super().setUp()
        self.delphi = self.host("delphi", {"bottles/dev.md": GOOD, "bottles/ci.md": GOOD})
        self.assertTrue(self.delphi.sync().pushed)
        self.merge_as_console("delphi")
        self.assertTrue(self.delphi.sync().held == ())

    def edit(self, rel: str, text: str) -> str:
        (self.delphi.root / rel).write_text(text)
        return record("edit", root=self.delphi.root)

    def branch(self) -> str:
        return _git(self.bare, "rev-parse", "refs/heads/hosts/delphi")

    def test_held_then_fixed_and_pushed(self) -> None:
        before = self.branch()
        bad = self.edit("bottles/dev.md", BAD)
        synced = self.delphi.sync()
        self.assertEqual("", synced.pushed)
        assert synced.held
        self.assertEqual(["bottles/dev.md"], [f.path for f in synced.held])
        self.assertIn("unterminated", synced.held[0].error)
        self.assertEqual(before, self.branch())  # not pushed
        self.assertEqual(bad, _git(self.delphi.root, "rev-parse", "HEAD"))  # but committed
        fixed = self.edit("bottles/dev.md", GOOD + "fixed\n")
        synced = self.delphi.sync()
        self.assertEqual((fixed, ()), (synced.pushed, synced.held))
        self.assertEqual(fixed, self.branch())
        self.assertEqual(fixed, _git(self.delphi.root, "rev-parse", lg.PUSHED_REF))

    def test_an_older_bad_commit_under_newer_good_ones_is_held(self) -> None:
        before = self.branch()
        self.edit("bottles/dev.md", BAD)
        self.edit("bottles/ci.md", GOOD + "fine\n")
        self.edit("bottles/new.md", GOOD)
        synced = self.delphi.sync()
        self.assertEqual(["bottles/dev.md"], [f.path for f in synced.held or ()])
        self.assertEqual(before, self.branch())

    def test_a_bad_file_deleted_since_is_fine(self) -> None:
        self.edit("bottles/dev.md", BAD)
        (self.delphi.root / "bottles/dev.md").unlink()
        record("gone", root=self.delphi.root)
        synced = self.delphi.sync()
        self.assertTrue(synced.pushed)
        self.assertEqual((), synced.held)

    def test_only_whats_new_since_the_last_push_is_checked(self) -> None:
        self.edit("bottles/ci.md", GOOD + "one\n")
        first = self.delphi.sync().pushed
        self.assertTrue(first)
        # main hasn't merged it, so only the commit after the push is checked.
        with mock.patch.object(lg, "unparseable", wraps=lg.unparseable) as check:
            self.edit("bottles/dev.md", GOOD + "two\n")
            self.assertTrue(self.delphi.sync().pushed)
        self.assertEqual(["bottles/dev.md"],
                         [p for p in check.call_args.args[0] if p])

    def test_a_fast_forward_clears_it(self) -> None:
        self.edit("bottles/dev.md", BAD)
        self.assertTrue(self.delphi.sync().held)
        _git(self.delphi.root, "reset", "-q", "--hard", "HEAD~1")  # the user backs it out
        delos = self.host("delos")
        delos.sync()
        self.merge_as_console("delos")
        synced = self.delphi.sync()
        self.assertTrue(synced.moved)
        self.assertEqual((), synced.held)


class HeldStore(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "db" / "host.db"  # pylint: disable=consider-using-with

    def test_kept_across_a_restart_since_it_began(self) -> None:
        store = LibraryHeldStore(self.path)
        self.assertIsNone(store.held())
        first = store.hold((HeldFile("bottles/dev.md", "bad"),))
        later = LibraryHeldStore(self.path).hold((HeldFile("agents/x.md", "worse"),))
        self.assertEqual(first.since, later.since)
        held = LibraryHeldStore(self.path).held()
        assert held is not None
        self.assertEqual((HeldFile("agents/x.md", "worse"),), held.files)
        self.assertTrue(store.clear())
        self.assertFalse(store.clear())

    def test_unreadable_files_are_none(self) -> None:
        store = LibraryHeldStore(self.path)
        store.hold(())
        with store._connection() as conn:
            conn.execute("UPDATE library_held SET files = 'nope'")
        held = store.held()
        assert held is not None
        self.assertEqual((), held.files)

    def test_it_is_a_host_db_table(self) -> None:
        self.assertIn("library_held", [t for s in host_db.stores(self.path) for t in s.TABLES])


class Notices(unittest.IsolatedAsyncioTestCase):
    """What the console hears: `library_held` on a change, and on each
    connection's first sync."""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.held = LibraryHeldStore(self.tmp / "host.db")
        self.sent: list[dict[str, Any]] = []
        self.fake = _FakeSync()
        self.enterContext(mock.patch.object(ll.log, "warn"))
        self.enterContext(mock.patch.object(ll.log, "info"))
        self.link = self.new_link()

    def new_link(self) -> ll.LibraryLink:
        async def send(msg: dict[str, Any]) -> None:
            self.sent.append(msg)
        return ll.LibraryLink(send, make_sync=lambda: self.fake,  # type: ignore[arg-type,return-value]
                              root=self.tmp / "no-repo", held=self.held,
                              conflicts=LibraryConflictStore(self.tmp / "host.db"))

    async def sync(self, synced: ll.Synced) -> None:
        self.fake.results = [synced]
        self.fake.calls = 0
        await self.link._announce(self.link.sync_now())

    async def test_held_then_parsing_again(self) -> None:
        bad = (HeldFile("bottles/dev.md", "yaml-subset: unterminated `[` on line 1"),)
        await self.sync(ll.Synced(main="m", held=bad))
        await self.sync(ll.Synced(main="m", held=bad))  # unchanged: not again
        await self.sync(ll.Synced(pushed="p", main="m", held=()))
        await self.sync(ll.Synced(main="p", held=()))
        self.assertEqual([
            {"type": "library_held", "files": [
                {"path": "bottles/dev.md", "error": "yaml-subset: unterminated `[` on line 1"}]},
            {"type": "library_held", "files": []},
            {"type": "library_pushed", "head": "p"},
        ], self.sent)
        self.assertIsNone(self.held.held())

    async def test_a_new_connection_hears_the_state_once(self) -> None:
        await self.sync(ll.Synced(main="m", held=()))
        await self.sync(ll.Synced(main="m", held=()))
        await self.sync(ll.Synced())  # not checked (no repo): nothing said
        self.assertEqual([{"type": "library_held", "files": []}], self.sent)
        self.held.hold((HeldFile("bottles/dev.md", "bad"),))
        self.link = self.new_link()
        await self.sync(ll.Synced(main="m", held=(HeldFile("bottles/dev.md", "bad"),)))
        self.assertEqual({"type": "library_held",
                          "files": [{"path": "bottles/dev.md", "error": "bad"}]}, self.sent[-1])

    async def test_a_launch_sync_announces_from_its_thread(self) -> None:
        self.fake.results = [ll.Synced(main="m", held=(HeldFile("bottles/dev.md", "bad"),))]
        self.link._loop = asyncio.get_running_loop()
        synced = await asyncio.to_thread(self.link.sync_now)
        self.link.announce_soon(synced)
        for _ in range(50):
            if self.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual("library_held", self.sent[0]["type"])

    async def test_an_unwritable_store_is_logged(self) -> None:
        with mock.patch.object(self.held, "held", side_effect=sqlite3.OperationalError("ro")):
            await self.sync(ll.Synced(main="m", held=()))
        self.assertEqual([], self.sent)

    async def test_library_off_clears_it(self) -> None:
        self.held.hold((HeldFile("bottles/dev.md", "bad"),))
        await self.link.handle({"type": "library_sync", "enabled": False})
        self.assertIsNone(self.held.held())


class HeldStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root))
        self.addCleanup(setattr, ll, "_active", None)

    def hold(self, *files: HeldFile) -> None:
        LibraryHeldStore(host_db_path()).hold(files)

    def link(self) -> ll.LibraryLink:
        async def send(msg: dict[str, Any]) -> None:
            del msg
        fake = _FakeSync()
        link = ll.LibraryLink(send, make_sync=lambda: fake)  # type: ignore[arg-type,return-value]
        link.enabled = True
        ll._active = link
        return link

    def test_status_says_what_doesnt_parse(self) -> None:
        self.link()
        self.hold(HeldFile("bottles/dev.md", "yaml-subset: unterminated `[` on line 1"))
        reply = host_state.get_pairing({})
        link = reply["link"]
        assert isinstance(link, dict)
        self.assertEqual(["bottles/dev.md"],
                         [f["path"] for f in link["library"]["held"]["files"]])
        with mock.patch.object(remote_cmd, "running_pairing",
                               return_value=(pairing.Pairing(), dict(link))):
            lines = remote_cmd.link_line().splitlines()
        self.assertEqual("library: dev doesn't parse; not synced (bottles/dev.md: "
                         "yaml-subset: unterminated `[` on line 1)", lines[1])

    def test_several_and_with_a_conflict(self) -> None:
        self.link()
        self.hold(HeldFile("bottles/dev.md", "bad"), HeldFile("agents/x.md", "worse"))
        LibraryConflictStore(host_db_path()).report(["bottles/ci.md"], "tip")
        lines = remote_cmd.library_line(ll.report()).splitlines()
        self.assertTrue(lines[0].startswith("library: conflict pending"))
        self.assertEqual("library: dev and agent x don't parse; not synced "
                         "(bottles/dev.md: bad; agents/x.md: worse)", lines[1])

    def test_off_and_unpaired_hold_nothing(self) -> None:
        self.hold(HeldFile("bottles/dev.md", "bad"))
        self.assertIsNone(ll.pending_held())
        self.assertEqual({"state": "off"}, ll.report())
        self.assertEqual("library: off", remote_cmd.library_line({"state": "off",
                                                                  "held": {"files": []}}))

    def test_an_unreadable_host_db_is_none(self) -> None:
        self.link()
        with mock.patch.object(ll, "_held_store", side_effect=OSError("gone")):
            self.assertIsNone(ll.pending_held())


if __name__ == "__main__":
    unittest.main()
