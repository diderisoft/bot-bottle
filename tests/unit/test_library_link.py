"""Unit: library sync (PRD 0131 chunk 2). A host pushes its `~/.bot-bottle`
repo's `main` to `hosts/<id>` on the account's library and fast-forwards to
the library's `main`; it never merges. A local bare repo stands in for the
console's library app (file transport), and fakes for its token and link."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import library_git as lg, library_link as ll
from bot_bottle.control.hostctl import library_upstream as lu
from bot_bottle.manifest import versions
from bot_bottle.control.hostctl.library_conflict import LibraryConflictStore
from bot_bottle.manifest.versions import record
from tests.unit import use_bottle_root


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class _Library(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bare = self.tmp / "library.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        self.journal: list[dict[str, Any]] = []
        self.tokens = 0

    def token(self) -> ll.Token:
        self.tokens += 1
        return ll.Token(url=str(self.bare), username="host", token="t",
                        expires=time.time() + 900)

    def host(self, name: str, files: dict[str, str] | None = None) -> ll.LibrarySync:
        root = self.tmp / name
        (root / "bottles").mkdir(parents=True)
        for rel, text in (files or {}).items():
            (root / rel).write_text(text)
        record(f"write on {name}", root=root)

        class _Audit:
            def __init__(self, sink: list[dict[str, Any]]) -> None:
                self.sink = sink

            def library_updated(self, **kw: Any) -> None:
                self.sink.append(kw)
        audit = _Audit(self.journal)
        return ll.LibrarySync(host_id=name, token_source=self.token, audit=lambda: audit,
                              root=root)

    def merge_as_console(self, host: str) -> str:
        """The console's merge: `main` merges the host's branch (unrelated
        histories too), by way of a scratch clone."""
        work = self.tmp / f"console-{time.monotonic_ns()}"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(work)], check=True,
                       capture_output=True)
        who = ("-c", "user.name=console", "-c", "user.email=c@c")
        branch = f"origin/hosts/{host}"
        has_main = subprocess.run(["git", "-C", str(work), "rev-parse", "--verify", "-q",
                                   "origin/main"], capture_output=True, check=False).returncode == 0
        if has_main:
            _git(work, "checkout", "-q", "-B", "main", "origin/main")
            _git(work, *who, "merge", "-q", "--no-edit", "--allow-unrelated-histories", branch)
        else:
            _git(work, "checkout", "-q", "-B", "main", branch)
        _git(work, "push", "-q", "origin", "main")
        return _git(work, "rev-parse", "HEAD")


class Syncing(_Library):
    def test_a_first_sync_pushes_the_hosts_history_to_its_branch(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "---\n---\n"})
        synced = delphi.sync()
        head = _git(delphi.root, "rev-parse", "HEAD")
        self.assertEqual(ll.Synced(pushed=head), synced)
        self.assertEqual(head, _git(self.bare, "rev-parse", "refs/heads/hosts/delphi"))

    def test_a_branch_holding_history_this_repo_lacks_is_replaced(self) -> None:
        # The repo made anew (or reset, or re-paired onto an earlier host's
        # branch): its branch holds commits this `main` doesn't build on.
        self.host("before", {"bottles/old.md": "---\n---\n"}).sync()
        anew = self.host("anew", {"bottles/dev.md": "---\n---\n"})
        anew.host_id = "before"
        synced = anew.sync()
        head = _git(anew.root, "rev-parse", "HEAD")
        self.assertEqual(head, synced.pushed)
        self.assertTrue(synced.replaced)
        self.assertEqual(head, _git(self.bare, "rev-parse", "refs/heads/hosts/before"))
        # A refusal for any other reason isn't forced past.
        self.assertFalse(lg._not_ahead(
            " ! [remote rejected] x -> hosts/before (pre-receive hook declined)"))

    def test_nothing_to_do_once_main_has_it(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "---\n---\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        self.assertEqual(ll.Synced(), delphi.sync())
        self.assertEqual([], self.journal)

    def test_another_hosts_change_fast_forwards_here_and_is_journalled(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        delos = self.host("delos")
        self.assertTrue(delos.sync().pushed)  # its own history, unrelated: pushed
        self.merge_as_console("delos")
        self.assertTrue(delos.sync().moved)  # and back with delphi's in it
        self.assertEqual("a\n", (delos.root / "bottles/dev.md").read_text())
        (delos.root / "bottles/dev.md").write_text("b\n")
        record("edited on delos", root=delos.root)
        delos.sync()
        self.merge_as_console("delos")
        old = _git(delphi.root, "rev-parse", "HEAD")
        self.assertEqual(ll.Synced(moved=True), delphi.sync())
        self.assertEqual("b\n", (delphi.root / "bottles/dev.md").read_text())
        event = self.journal[-1]
        self.assertEqual(old, event["old"])
        self.assertEqual(_git(delphi.root, "rev-parse", "HEAD"), event["new"])
        self.assertEqual(["bottles/dev.md"], event["changed"])
        self.assertIn("bot-bottle", event["authors"])

    def test_commits_main_lacks_are_pushed_not_merged(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        (delphi.root / "bottles/dev.md").write_text("c\n")  # a hand edit
        synced = delphi.sync()
        self.assertTrue(synced.pushed)
        self.assertEqual(synced.pushed, _git(self.bare, "rev-parse", "refs/heads/hosts/delphi"))
        self.assertEqual("edited by hand", _git(delphi.root, "log", "-1", "--format=%s"))
        self.assertNotEqual(synced.pushed, _git(self.bare, "rev-parse", "refs/heads/main"))

    def test_a_repo_with_no_commits_takes_the_library(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", str(fresh)], check=True)
        audit = mock.Mock()
        sync = ll.LibrarySync(host_id="fresh", token_source=self.token, root=fresh,
                              audit=lambda: audit)
        @contextlib.contextmanager
        def no_commit(_root: Path) -> Generator[str]:  # no .gitignore of bot-bottle's yet
            yield ""
        with mock.patch.object(lg, "writing", no_commit):
            self.assertEqual(ll.Synced(moved=True), sync.sync())
        self.assertEqual("a\n", (fresh / "bottles/dev.md").read_text())
        kw = audit.library_updated.call_args.kwargs
        self.assertEqual(("", ["bottles/dev.md"], ["bottles/dev.md"]),
                         (kw["old"], kw["changed"], kw["added"]))

    def test_an_unreachable_library_is_an_error(self) -> None:
        delphi = self.host("delphi")
        delphi.token_source = lambda: ll.Token(url=str(self.tmp / "nope.git"), username="h",
                                               token="t", expires=time.time() + 900)
        with self.assertRaises(ll.LibraryError):
            delphi.sync()

    def test_no_repo_no_sync(self) -> None:
        sync = ll.LibrarySync(host_id="x", token_source=self.token, root=self.tmp / "empty")
        self.assertEqual(ll.Synced(), sync.sync())

    def test_a_token_is_reused_until_it_nearly_expires(self) -> None:
        delphi = self.host("delphi")
        delphi.sync()
        delphi.sync()
        self.assertEqual(1, self.tokens)
        assert delphi._token is not None  # pylint: disable=protected-access
        delphi._token = ll.Token(url=str(self.bare), username="host",  # pylint: disable=protected-access
                                 token="t", expires=time.time() + 30)
        delphi.sync()
        self.assertEqual(2, self.tokens)

    def test_the_token_reaches_git_only_through_the_environment(self) -> None:
        delphi = self.host("delphi")
        with mock.patch("subprocess.Popen", wraps=subprocess.Popen) as run:
            delphi.sync()
        fetch = next(c for c in run.call_args_list if "fetch" in c.args[0])
        self.assertEqual("t", fetch.kwargs["env"]["BB_LIBRARY_TOKEN"])
        self.assertNotIn("t", [a for a in fetch.args[0] if a == "t"])
        self.assertNotIn("library", _git(delphi.root, "config", "--list"))


class ConsoleToken(unittest.TestCase):
    def paired(self) -> Any:
        p = mock.Mock(url="https://console.example/", host_id="h1")
        p.require.return_value = p
        p.tokens.return_value = ("access", "refresh")
        return p

    def test_asks_the_console_with_the_access_token(self) -> None:
        reply = {"url": "https://lib/1.git", "username": "host", "token": "tok",
                 "expires_at": "2030-01-01T00:00:00Z"}
        with mock.patch.object(ll.pairing, "load_pairing", return_value=self.paired()), \
                mock.patch.object(lg, "_post", return_value=reply) as post:
            token = ll.console_token()
        self.assertEqual(("https://lib/1.git", "tok"), (token.url, token.token))
        self.assertEqual(("https://console.example/api/v1/hosts/library-token", {}, "access"),
                         post.call_args.args[:3])
        self.assertGreater(token.expires, time.time())

    def test_refreshes_once_on_a_401(self) -> None:
        expired = urllib.error.HTTPError("u", 401, "expired", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        replies: list[Any] = [expired, {"access_token": "new"},
                              {"url": "https://lib/1.git", "token": "t", "expires_at": "bad"}]

        def post(*_a: Any, **_k: Any) -> dict[str, Any]:
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        store = mock.Mock()
        with mock.patch.object(ll.pairing, "load_pairing", return_value=self.paired()), \
                mock.patch.object(ll.pairing, "store", return_value=store), \
                mock.patch.object(lg, "_post", side_effect=post):
            token = ll.console_token()
        store.update.assert_called_once_with({"access_token": "new"})
        self.assertEqual("t", token.token)

    def test_a_library_thats_off_or_an_account_gone_isnt_retried_soon(self) -> None:
        for code in (401, 404):
            refused = urllib.error.HTTPError("u", code, "no", {}, io.BytesIO(b""))  # type: ignore[arg-type]
            paired = self.paired()
            paired.tokens.return_value = ("access", "")
            with mock.patch.object(ll.pairing, "load_pairing", return_value=paired), \
                    mock.patch.object(lg, "_post", side_effect=refused):
                with self.assertRaises(ll.LibraryError) as caught:
                    ll.console_token()
            self.assertTrue(caught.exception.lasting)

    def test_a_url_that_isnt_https_is_refused(self) -> None:
        with mock.patch.object(ll.pairing, "load_pairing", return_value=self.paired()), \
                mock.patch.object(lg, "_post", return_value={"url": "--upload-pack=id",
                                                             "token": "t"}):
            with self.assertRaises(ll.LibraryError):
                ll.console_token()

    def test_a_refusal_is_a_library_error(self) -> None:
        with mock.patch.object(ll.pairing, "load_pairing", return_value=self.paired()), \
                mock.patch.object(lg, "_post", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(ll.LibraryError):
                ll.console_token()

    def test_post_sends_json_with_the_bearer(self) -> None:
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(json.dumps({"ok": 1}).encode())
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            self.assertEqual({"ok": 1}, lg._post("https://x/y", {"a": 1}, "tok"))  # pylint: disable=protected-access
        req = urlopen.call_args.args[0]
        self.assertEqual("Bearer tok", req.get_header("Authorization"))


class _FakeSync:
    def __init__(self, results: list[ll.Synced] | None = None, *, delay: float = 0.0,
                 error: Exception | None = None) -> None:
        self.results = results or [ll.Synced()]
        self.calls = 0
        self.delay = delay
        self.error = error

    def sync(self) -> ll.Synced:
        self.calls += 1
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.results[min(self.calls, len(self.results)) - 1]

    upstream_notes: tuple[str, ...] = ()

    def repo(self) -> lu.Repo:
        return lu.Repo(Path("/nonexistent"), {})

    def heads(self) -> tuple[str, str]:
        return "tip", ""

    def branch_tip(self) -> str:
        return "tip"

    def contains(self, newer: str, older: str) -> bool:
        return newer == older


class Link(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fake = _FakeSync([ll.Synced(pushed="abc")])

        async def send(msg: dict[str, Any]) -> None:
            self.sent.append(msg)
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.tmp.cleanup)
        self.link = ll.LibraryLink(send, make_sync=lambda: self.fake,  # type: ignore[arg-type,return-value]
                                   root=Path(self.tmp.name), conflicts=LibraryConflictStore(
                                       Path(self.tmp.name) / "db" / "host.db"))
        self.addCleanup(setattr, ll, "_active", None)

    async def settle(self) -> None:
        for _ in range(50):
            task = self.link._task  # pylint: disable=protected-access
            if task is None or task.done():
                return
            await asyncio.sleep(0.01)

    async def test_enabled_syncs_and_announces_a_push(self) -> None:
        await self.link.handle({"type": "library_sync", "enabled": True})
        await self.settle()
        self.assertEqual([{"type": "library_pushed", "head": "abc"}], self.sent)
        self.assertIs(self.link, ll._active)  # pylint: disable=protected-access

    async def test_disabled_never_syncs(self) -> None:
        await self.link.handle({"type": "library_sync", "enabled": False})
        await self.link.handle({"type": "library_moved", "head": "x"})
        await self.link.tick()
        await self.settle()
        self.assertEqual(0, self.fake.calls)
        self.assertIsNone(ll._active)  # pylint: disable=protected-access

    async def test_moved_syncs(self) -> None:
        self.link.enabled = True
        await self.link.handle({"type": "library_moved", "head": "x"})
        await self.settle()
        self.assertEqual(1, self.fake.calls)

    async def test_a_conflict_is_kept_until_main_moves_here(self) -> None:
        await self.link.handle({"type": "library_conflict", "files": ["bottles/dev.md"]})
        self.assertEqual(["bottles/dev.md"], self.link.conflict)
        self.fake.results = [ll.Synced(moved=True, main="tip")]
        self.link.enabled = True
        self.link.sync_now()
        self.assertEqual([], self.link.conflict)

    async def test_a_conflicting_proposal_is_kept_until_merged_or_replaced(self) -> None:
        # The branch holds a proposal (PRD 0131): `main` moving here doesn't
        # resolve it; `main` holding it, or this host's own push over it, does.
        self.fake.branch_tip = lambda: "proposal"  # type: ignore[method-assign]
        await self.link.handle({"type": "library_conflict", "files": ["bottles/basic.md"]})
        self.link.enabled = True
        self.fake.results = [ll.Synced(moved=True, main="other")]
        self.link.sync_now()
        self.assertEqual(["bottles/basic.md"], self.link.conflict)
        self.fake.results = [ll.Synced(main="proposal")]
        self.fake.calls = 0
        self.link.sync_now()
        self.assertEqual([], self.link.conflict)
        await self.link.handle({"type": "library_conflict", "files": ["bottles/basic.md"]})
        self.fake.results = [ll.Synced(pushed="mine", main="other", replaced=True)]
        self.fake.calls = 0
        self.link.sync_now()
        self.assertEqual([], self.link.conflict)

    async def test_a_burst_is_one_sync_and_one_more(self) -> None:
        self.fake.delay = 0.1
        self.link.enabled = True
        self.link.schedule()
        await asyncio.sleep(0.03)  # it's syncing now
        for _ in range(4):
            self.link.schedule()
        await self.settle()
        self.assertEqual(2, self.fake.calls)

    async def test_a_new_local_commit_is_noticed(self) -> None:
        git = Path(self.tmp.name) / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        self.link.enabled = True
        await self.link.tick()
        await self.settle()
        await self.link.tick()  # unchanged: no sync
        await self.settle()
        self.assertEqual(1, self.fake.calls)

    async def test_a_failed_sync_only_warns(self) -> None:
        self.fake.error = ll.LibraryError("down")
        self.link.enabled = True
        with mock.patch.object(ll.log, "warn") as warn:
            self.link.schedule()
            await self.settle()
        warn.assert_called_once()
        self.assertEqual([], self.sent)


class BeforeALaunch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.sent: list[dict[str, Any]] = []

        async def send(msg: dict[str, Any]) -> None:
            self.sent.append(msg)
        self.send = send
        self.addCleanup(setattr, ll, "_active", None)
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(tmp.name)))  # the host DB's conflict

    def link(self, fake: _FakeSync) -> ll.LibraryLink:
        link = ll.LibraryLink(self.send, make_sync=lambda: fake)  # type: ignore[arg-type,return-value]
        link.enabled = True
        link._loop = asyncio.get_running_loop()  # pylint: disable=protected-access
        ll._active = link  # pylint: disable=protected-access
        return link

    async def test_not_synced_here_says_nothing(self) -> None:
        self.assertEqual("", await asyncio.to_thread(ll.sync_for_launch))

    async def test_a_sync_in_time_says_nothing_and_announces_its_push(self) -> None:
        self.link(_FakeSync([ll.Synced(pushed="abc")]))
        self.assertEqual("", await asyncio.to_thread(ll.sync_for_launch))
        await asyncio.sleep(0.05)
        self.assertEqual([{"type": "library_pushed", "head": "abc"}], self.sent)

    async def test_a_failure_says_it_launches_the_local_copy(self) -> None:
        self.link(_FakeSync(error=ll.LibraryError("refused")))
        note = await asyncio.to_thread(ll.sync_for_launch)
        self.assertIn("couldn't sync (refused)", note)

    async def test_a_slow_console_says_so(self) -> None:
        self.link(_FakeSync(delay=0.3))
        note = await asyncio.to_thread(ll.sync_for_launch, 0.05)
        self.assertIn("no answer in time", note)

    async def test_the_host_route_and_the_cli(self) -> None:
        from bot_bottle.cli.launch_client import library_sync  # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl import host_state, runner  # pylint: disable=import-outside-toplevel
        with mock.patch.object(ll, "sync_for_launch", return_value="note"):
            self.assertEqual({"note": "note"}, host_state.library_sync({}))
        client = mock.Mock()
        client.post.return_value = {"note": "used the local copy"}
        self.assertEqual("used the local copy", library_sync(client))
        client.post.side_effect = runner.HostControlError("old host controller")
        self.assertEqual("", library_sync(client))


class Names(unittest.TestCase):
    def test_none_shadows_a_message_the_link_already_handles(self) -> None:
        # `library` is PRD 0109's request for the host's agents and bottles.
        from bot_bottle.control.hostctl import remote_actions  # pylint: disable=import-outside-toplevel
        self.assertNotIn("library", ll.LibraryLink.REQUESTS)
        self.assertEqual(set(), ll.LibraryLink.REQUESTS & set(remote_actions.QUERIES))


class Journal(unittest.TestCase):
    def test_library_updated_is_a_security_event(self) -> None:
        from bot_bottle.control.hostctl.audit_events import HostAudit  # pylint: disable=import-outside-toplevel
        audit = HostAudit.__new__(HostAudit)
        with mock.patch.object(HostAudit, "record") as rec, \
                mock.patch.object(HostAudit, "_now", return_value="2026-11-14T00:00:00Z",
                                  create=True):
            audit.library_updated(old="a", new="b", changed=["bottles/dev.md"],
                                  authors=["delos"])
        event = rec.call_args.args[0]
        self.assertEqual(("library.updated", "security"), (event.type, event.sensitivity))
        self.assertEqual("library.updated", event.action)
        self.assertEqual({"from": "a", "to": "b", "changed": ["bottles/dev.md"], "added": [],
                          "deleted": [], "authors": ["delos"]}, event.payload)


class Safety(_Library):
    def test_the_fast_forward_waits_for_a_bot_bottle_write(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        delos = self.host("delos")
        delos.sync()
        self.merge_as_console("delos")
        done = threading.Event()
        # pylint: disable-next=protected-access
        with versions._locked(delos.root):  # a CLI `record`, say
            worker = threading.Thread(target=lambda: (delos.sync(), done.set()))
            worker.start()
            self.assertFalse(done.wait(0.5))
        worker.join(10)
        self.assertTrue(done.is_set())
        self.assertEqual("a\n", (delos.root / "bottles/dev.md").read_text())

    def test_a_hand_edit_is_committed_before_the_fast_forward(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        (delphi.root / "bottles/new.md").write_text("mine\n")  # not committed yet
        synced = delphi.sync()
        self.assertTrue(synced.pushed)
        self.assertEqual("mine\n", (delphi.root / "bottles/new.md").read_text())
        self.assertIn("bottles/new.md", _git(self.bare, "ls-tree", "-r", "--name-only",
                                             "refs/heads/hosts/delphi"))

    def test_a_symlink_in_the_library_checks_out_as_a_plain_file(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        work = self.tmp / "evil"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.bare), str(work)],
                       check=True, capture_output=True)
        os.symlink("../../../.ssh/id_ed25519", work / "bottles" / "steal.md")
        _git(work, "add", "-A")
        _git(work, "-c", "user.name=console", "-c", "user.email=c@c", "commit", "-q", "-m", "x")
        _git(work, "push", "-q", "origin", "HEAD:main")
        self.assertTrue(delphi.sync().moved)
        steal = delphi.root / "bottles" / "steal.md"
        self.assertFalse(steal.is_symlink())
        self.assertEqual("../../../.ssh/id_ed25519", steal.read_text())
        self.assertEqual(["bottles/steal.md"], self.journal[-1]["added"])

    def test_a_deleted_bottle_is_journalled_as_deleted(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n", "bottles/old.md": "o\n"})
        delphi.sync()
        self.merge_as_console("delphi")
        delos = self.host("delos")
        delos.sync()
        self.merge_as_console("delos")
        delos.sync()
        (delos.root / "bottles/old.md").unlink()
        record("removed old", root=delos.root)
        delos.sync()
        self.merge_as_console("delos")
        delphi.sync()
        self.assertEqual((["bottles/old.md"], []),
                         (self.journal[-1]["deleted"], self.journal[-1]["added"]))

    def test_a_commit_made_meanwhile_is_left_for_the_next_push(self) -> None:
        delphi = self.host("delphi", {"bottles/dev.md": "a\n"})
        head = _git(delphi.root, "rev-parse", "HEAD")
        real = delphi._git  # pylint: disable=protected-access

        def git(*args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if "push" in args:  # a CLI write lands while the push starts
                (delphi.root / "bottles/dev.md").write_text("later\n")
                record("later", root=delphi.root)
            return real(*args, **kw)
        with mock.patch.object(delphi, "_git", side_effect=git):
            self.assertEqual(head, delphi.sync().pushed)
        self.assertEqual(head, _git(self.bare, "rev-parse", "refs/heads/hosts/delphi"))


class Failures(unittest.TestCase):
    def test_git_refusals_are_told_apart(self) -> None:
        deleted = lg._failure("fetch", "fatal: unable to access 'u': "  # pylint: disable=protected-access
                              "The requested URL returned error: 403")
        self.assertTrue(deleted.lasting)
        self.assertIn("deleted", str(deleted))
        big = lg._failure("push", "error: RPC failed; HTTP 413 curl 22 The requested "  # pylint: disable=protected-access
                          "URL returned error: 413")
        self.assertTrue(big.lasting)
        self.assertIn("too large", str(big))
        fsck = lg._failure("push", " ! [remote rejected] abc -> hosts/h (unpacker error)")  # pylint: disable=protected-access
        self.assertTrue(fsck.lasting)
        down = lg._failure("fetch", "fatal: unable to access 'u': Could not resolve host")  # pylint: disable=protected-access
        self.assertFalse(down.lasting)

    def test_a_refused_token_is_replaced_once(self) -> None:
        tokens = iter("abc")
        sync = ll.LibrarySync(host_id="h", root=Path("/nonexistent"), token_source=lambda: ll.Token(
            url="https://lib.example/1.git", username="host", token=next(tokens),
            expires=time.time() + 900))
        refused = subprocess.CompletedProcess([], 128, "", "fatal: Authentication failed for 'u'")
        with mock.patch.object(sync, "_git", return_value=refused) as git:
            token, result = sync._remote("fetch", "u")  # pylint: disable=protected-access
        self.assertEqual(("b", 128), (token.token, result.returncode))
        self.assertEqual(["a", "b"], [c.kwargs["token"].token for c in git.call_args_list])
        self.assertIsNone(sync._token)  # pylint: disable=protected-access
        self.assertEqual(lg.NETWORK_SECONDS, git.call_args.kwargs["timeout"])

    def test_a_stuck_git_is_killed_with_what_it_started(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ll.LibraryError) as caught:
            lg._run(["sh", "-c", "sleep 30 & sleep 30"], {}, 0.2)  # pylint: disable=protected-access
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("no answer", str(caught.exception))

    def test_the_helper_answers_only_the_librarys_host(self) -> None:
        config = lg._credential_config("https://lib.example:8443/1.git")  # pylint: disable=protected-access
        env = {**versions.git_env(), "BB_LIBRARY_USER": "host", "BB_LIBRARY_TOKEN": "tok",
               "HOME": tempfile.gettempdir()}

        def fill(host: str) -> str:
            return subprocess.run(["git", *config, "credential", "fill"], env=env,
                                  input=f"protocol=https\nhost={host}\n\n", text=True,
                                  capture_output=True, check=False,
                                  cwd=tempfile.gettempdir()).stdout
        self.assertIn("password=tok", fill("lib.example:8443"))
        self.assertNotIn("tok", fill("evil.example"))

    def test_only_an_https_library_url_is_taken(self) -> None:
        for good in ("https://library.bot-bottle.app/1.git", "http://localhost:8080/1.git"):
            self.assertEqual(good, lg._library_url(good))  # pylint: disable=protected-access
        for bad in ("--upload-pack=touch /tmp/x", "ext::sh -c id", "file:///etc",
                    "http://library.example/1.git", "/srv/1.git"):
            with self.assertRaises(ll.LibraryError):
                lg._library_url(bad)  # pylint: disable=protected-access


class Retrying(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.tmp.cleanup)
        self.fake = _FakeSync(error=ll.LibraryError("down"))

        async def send(_msg: dict[str, Any]) -> None:
            return None
        self.link = ll.LibraryLink(send, make_sync=lambda: self.fake,  # type: ignore[arg-type,return-value]
                                   root=Path(self.tmp.name))
        self.link.enabled = True
        self.addCleanup(setattr, ll, "_active", None)

    async def settle(self) -> None:
        for _ in range(50):
            task = self.link._task  # pylint: disable=protected-access
            if task is None or task.done():
                return
            await asyncio.sleep(0.01)

    async def test_a_failure_is_tried_again_later_and_logged_once(self) -> None:
        with mock.patch.object(ll.log, "warn") as warn:
            for _ in range(3):
                with contextlib.suppress(ll.LibraryError):
                    self.link.sync_now()
        warn.assert_called_once()
        self.assertEqual("down", self.link.error)
        first, longest, _ = ll.RETRY_SECONDS
        self.assertAlmostEqual(time.monotonic() + min(first * 4, longest), self.link.retry_at,
                               delta=1)
        await self.link.tick()  # not due yet, nothing new: no sync
        await self.settle()
        self.assertEqual(3, self.fake.calls)
        self.link.retry_at = time.monotonic() - 1
        self.fake.error = None
        with mock.patch.object(ll.log, "info") as info:
            await self.link.tick()
            await self.settle()
        self.assertEqual(4, self.fake.calls)
        info.assert_called_once_with("library: synced again")
        self.assertEqual(("", 0.0), (self.link.error, self.link.retry_at))

    async def test_a_lasting_refusal_waits_longest(self) -> None:
        self.fake.error = ll.LibraryError("deleted", lasting=True)
        with mock.patch.object(ll.log, "warn"), contextlib.suppress(ll.LibraryError):
            self.link.sync_now()
        self.assertAlmostEqual(time.monotonic() + ll.RETRY_SECONDS[2], self.link.retry_at,
                               delta=1)

    async def test_a_launch_doesnt_wait_while_a_retry_is_pending(self) -> None:
        with mock.patch.object(ll.log, "warn"), contextlib.suppress(ll.LibraryError):
            self.link.sync_now()
        ll._active = self.link  # pylint: disable=protected-access
        started = time.monotonic()
        note = await asyncio.to_thread(ll.sync_for_launch, 5.0)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("couldn't sync (down)", note)
        self.assertEqual(1, self.fake.calls)

    async def test_launches_meanwhile_share_one_sync(self) -> None:
        self.fake.error, self.fake.delay = None, 0.3
        ll._active = self.link  # pylint: disable=protected-access
        notes = await asyncio.gather(*(asyncio.to_thread(ll.sync_for_launch, 2.0)
                                       for _ in range(3)))
        self.assertEqual(["", "", ""], notes)
        self.assertEqual(1, self.fake.calls)

    async def test_its_own_commits_dont_set_off_another_sync(self) -> None:
        self.fake.error = None
        self.fake.results = [ll.Synced(moved=True, stamp=42.0)]
        self.link.schedule()
        await self.settle()
        self.assertEqual(42.0, self.link._seen)  # pylint: disable=protected-access

    async def test_an_unexpected_error_doesnt_end_the_link(self) -> None:
        self.fake.error = RuntimeError("bug")
        with mock.patch.object(ll.log, "warn") as warn:
            self.link.schedule()
            await self.settle()
        self.assertIn("bug", warn.call_args.args[0])

    async def test_closing_the_link_stops_launch_syncs(self) -> None:
        await self.link.handle({"type": "library_sync", "enabled": True})
        self.assertIs(self.link, ll._active)  # pylint: disable=protected-access
        self.link.close()
        self.assertIsNone(ll._active)  # pylint: disable=protected-access
        self.assertFalse(self.link.enabled)
        self.assertEqual("", ll.sync_for_launch())


if __name__ == "__main__":
    unittest.main()
