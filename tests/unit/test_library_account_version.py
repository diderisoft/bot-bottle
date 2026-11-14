"""The account's version (PRD 0131 chunk 6): `bot-bottle.lock` tracked but
never written by a host, the built-ins it reports, the built-ins it keeps,
and copies of built-ins merging a release's changes by way of a proposal on
the host's branch. Real git repos throughout; the console's merge is its
`git merge-tree` (as `bb_library.repos.Libraries._merge` runs it)."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import remote as remote_cmd
from bot_bottle.control.hostctl import (builtins_cache as bc, library_git as lg,
                                        library_link as ll, library_upstream as lu)
from bot_bottle.manifest import parse_check
from bot_bottle.manifest.account_version import (LOCK_FILE, copied_from, is_release,
                                                 lock_version, mark, packaged, packaged_files,
                                                 release_key)
from bot_bottle.manifest.extends import resolve_bottles
from bot_bottle.manifest.agent import agent_from_frontmatter
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.manifest.versions import pending_edits, record
from tests.unit.test_library_link import _git, _Library

INSTALLED_RELEASE = bc.installed_release

OLD, NEW = "0.1.16", "0.1.17"
BASIC_OLD = "---\negress:\n  routes:\n    - host: a.example\n---\nThe basic bottle.\n"
BASIC_NEW = ("---\negress:\n  routes:\n    - host: a.example\n    - host: b.example\n---\n"
             "The basic bottle.\n")


class Forms(unittest.TestCase):
    """The lock and the marker, as the console writes and reads them."""

    def test_the_lock(self) -> None:
        self.assertEqual("0.1.16", lock_version('version = "0.1.16"\n'))
        self.assertEqual("1.2.3", lock_version('# the account\n\n  version = "1.2.3"  \n'))
        for text in ("", 'version = "main"\n', "version = 0.1.16\n", 'name = "0.1.16"\n'):
            self.assertEqual("", lock_version(text), text)
        self.assertTrue(is_release("0.1.16"))
        self.assertFalse(is_release("0.1.16rc1"))
        self.assertLess(release_key("0.1.9"), release_key("0.1.10"))

    def test_the_marker(self) -> None:
        self.assertEqual("---\ncopied_from: basic@0.1.16\n# x\n---\nbody\n",
                         mark("---\n# x\n---\nbody\n", "basic", "0.1.16"))
        self.assertEqual("---\ncopied_from: claude@0.1.16\n---\nbody\n",
                         mark("body\n", "claude", "0.1.16"))
        self.assertEqual(("basic", "0.1.16"), copied_from(mark(BASIC_OLD, "basic", OLD)))
        self.assertEqual(("basic", "0.1.16"), copied_from("---\ncopied_from:basic@0.1.16 \n"))
        for text in ("copied_from: basic@0.1.16\n", "---\nenv: {}\ncopied_from: basic@0.1.16\n",
                     "---\ncopied_from: basic@main\n", "---\ncopied_from: ../x@0.1.16\n"):
            self.assertIsNone(copied_from(text), text)

    def test_a_copy_parses_and_launches_as_its_built_in(self) -> None:
        bottle = mark(packaged("bottles", "basic") or "", "basic", OLD)
        self.assertEqual("", parse_check.problem("bottles/basic.md", bottle))
        fm, _ = parse_frontmatter(bottle)
        resolved = resolve_bottles({"basic": dict(fm)})["basic"]
        fm0, _ = parse_frontmatter(packaged("bottles", "basic") or "")
        self.assertEqual(resolve_bottles({"basic": dict(fm0)})["basic"], resolved)
        agent = mark(packaged("agents", "claude") or "", "claude", OLD)
        self.assertEqual("", parse_check.problem("agents/claude.md", agent))
        fm, body = parse_frontmatter(agent)
        agent_from_frontmatter("claude", dict(fm), body)

    def test_the_packaged_built_ins(self) -> None:
        files = packaged_files()
        self.assertIn("bottles/basic.md", files)
        self.assertIn("agents/claude.md", files)
        self.assertIsNone(packaged("bottles", "../agents/claude"))
        self.assertIsNone(packaged("nope", "basic"))
        self.assertIsNone(packaged("bottles", "no-such-bottle"))


class Cache(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fetched: list[tuple[str, str, str]] = []
        self.remote: dict[tuple[str, str, str], str | None] = {}
        self.enterContext(mock.patch.object(bc, "installed_release", return_value=NEW))

    def fetch(self, kind: str, name: str, version: str) -> str | None:
        self.fetched.append((kind, name, version))
        key = (kind, name, version)
        if key not in self.remote:
            raise bc.BuiltinUnavailable("offline")
        return self.remote[key]

    def test_the_installed_release_is_kept_on_start(self) -> None:
        self.assertEqual(NEW, bc.fill(self.root))
        kept = self.root / "builtins" / NEW
        self.assertEqual(packaged("bottles", "basic"), (kept / "bottles/basic.md").read_text())
        self.assertTrue((kept / "agents/claude.md").is_file())
        (kept / "bottles/basic.md").write_text("kept\n")
        bc.fill(self.root)  # what's kept stays
        self.assertEqual("kept\n", (kept / "bottles/basic.md").read_text())
        with mock.patch.object(bc, "installed_release", return_value=""):
            self.assertEqual("", bc.fill(self.root))

    def test_a_release_is_packaged_kept_or_fetched_and_kept(self) -> None:
        builtins = bc.Builtins(self.root, self.fetch)
        self.assertEqual(packaged("bottles", "basic"), builtins.at("bottles", "basic", NEW))
        (self.root / "builtins" / OLD / "bottles").mkdir(parents=True)
        (self.root / "builtins" / OLD / "bottles/basic.md").write_text(BASIC_OLD)
        self.assertEqual(BASIC_OLD, builtins.at("bottles", "basic", OLD))
        self.remote[("agents", "claude", OLD)] = "claude\n"
        self.assertEqual("claude\n", builtins.at("agents", "claude", OLD))
        self.assertEqual("claude\n", (self.root / "builtins" / OLD / "agents/claude.md")
                         .read_text())
        self.assertEqual("claude\n", builtins.at("agents", "claude", OLD))
        self.assertEqual([("agents", "claude", OLD)], self.fetched)

    def test_a_release_without_it_or_out_of_reach(self) -> None:
        builtins = bc.Builtins(self.root, self.fetch)
        self.remote[("agents", "codex", OLD)] = None
        self.assertIsNone(builtins.at("agents", "codex", OLD))
        self.assertIsNone(builtins.at("agents", "codex", OLD))
        self.assertEqual(1, len(self.fetched))  # its absence is remembered
        with self.assertRaisesRegex(bc.BuiltinUnavailable, "offline"):
            builtins.at("bottles", "basic", OLD)
        for kind, name, version in (("nope", "basic", OLD), ("bottles", "../x", OLD),
                                    ("bottles", "basic", "main")):
            self.assertIsNone(builtins.at(kind, name, version))

    def test_github_out_of_reach_is_tried_again_later(self) -> None:
        now = [0.0]
        builtins = bc.Builtins(self.root, self.fetch, clock=lambda: now[0])
        with self.assertRaisesRegex(bc.BuiltinUnavailable, "offline"):
            builtins.at("bottles", "basic", OLD)
        self.remote[("bottles", "basic", OLD)] = BASIC_OLD
        with self.assertRaisesRegex(bc.BuiltinUnavailable, "offline"):
            builtins.at("bottles", "basic", OLD)  # not asked again yet
        self.assertEqual(1, len(self.fetched))
        now[0] += bc.RETRY_SECONDS
        self.assertEqual(BASIC_OLD, builtins.at("bottles", "basic", OLD))

    def test_fetching_a_release_from_its_tag(self) -> None:
        def answer(body: bytes) -> mock.MagicMock:
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = body
            return resp
        with mock.patch("urllib.request.urlopen", return_value=answer(b"basic\n")) as get:
            self.assertEqual("basic\n", bc.fetch("bottles", "basic", OLD))
        self.assertEqual("https://raw.githubusercontent.com/diderisoft/bot-bottle/v0.1.16/"
                         "bot_bottle/manifest/builtin/basic.md", get.call_args.args[0])
        with mock.patch("urllib.request.urlopen", return_value=answer(b"c\n")) as get:
            bc.fetch("agents", "claude", OLD)
        self.assertTrue(get.call_args.args[0].endswith("/manifest/builtin_agents/claude.md"))

        def refuse(code: int) -> urllib.error.HTTPError:
            return urllib.error.HTTPError("u", code, "x", None, io.BytesIO())  # type: ignore[arg-type]
        with mock.patch("urllib.request.urlopen", side_effect=refuse(404)):
            self.assertIsNone(bc.fetch("bottles", "basic", OLD))
        for failure, why in ((refuse(500), "500"), (urllib.error.URLError("down"), "reach"),
                             (None, "too large"), (None, "UTF-8")):
            body = b"x" * (300 * 1024) if why == "too large" else b"\xff"
            with mock.patch("urllib.request.urlopen", side_effect=failure,
                            return_value=answer(body)), \
                    self.assertRaisesRegex(bc.BuiltinUnavailable, why):
                bc.fetch("bottles", "basic", OLD)
        for kind, name, version in (("nope", "basic", OLD), ("bottles", "../x", OLD),
                                    ("bottles", "basic", "0.1.16/../../x")):
            with mock.patch("urllib.request.urlopen") as get, self.assertRaises(ValueError):
                bc.fetch(kind, name, version)
            get.assert_not_called()

    def test_installed_release(self) -> None:
        with mock.patch.object(bc.resources, "is_source_checkout", return_value=True):
            self.assertEqual("", INSTALLED_RELEASE())
        with mock.patch.object(bc.resources, "is_source_checkout", return_value=False), \
                mock.patch.object(bc, "install_version", return_value=("0.1.17", "")):
            self.assertEqual("0.1.17", INSTALLED_RELEASE())
        with mock.patch.object(bc.resources, "is_source_checkout", return_value=False), \
                mock.patch.object(bc, "install_version", return_value=("unknown", "")):
            self.assertEqual("", INSTALLED_RELEASE())


class _Account(_Library):
    """A library with a console that copies built-ins and moves the lock."""

    def setUp(self) -> None:
        super().setUp()
        self.release: dict[tuple[str, str], str | None] = {("bottles", "basic"): BASIC_NEW}
        self.enterContext(mock.patch.object(lu, "packaged", side_effect=self.packaged))
        self.old: dict[tuple[str, str, str], str | None] = {
            ("bottles", "basic", OLD): BASIC_OLD}

    def packaged(self, kind: str, name: str) -> str | None:
        """The installed release's built-in, as the test says it is."""
        return self.release.get((kind, name))

    def fetch(self, kind: str, name: str, version: str) -> str | None:
        key = (kind, name, version)
        if key not in self.old:
            raise bc.BuiltinUnavailable("offline")
        return self.old[key]

    def on(self, name: str, installed: str = NEW) -> lg.LibrarySync:
        sync = self.host(name, {f"bottles/{name}.md": "---\n---\n"})
        sync.installed = lambda: installed
        sync.builtins = bc.Builtins(self.tmp / name, self.fetch)
        return sync

    def console(self, files: dict[str, str], message: str) -> str:
        """A console commit on `main` (Copy to library, the lock, an edit)."""
        work = self.tmp / f"console-{time.monotonic_ns()}"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.bare), str(work)],
                       check=True, capture_output=True)
        for rel, text in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text)
        _git(work, "add", "-A", "-f")
        _git(work, "-c", "user.name=console", "-c", "user.email=c@c", "commit", "-q",
             "-m", message)
        _git(work, "push", "-q", "origin", "main")
        return _git(work, "rev-parse", "HEAD")

    def merge_tree(self, host: str) -> list[str]:
        """The console's merge of a host's branch: a merge commit when clean,
        else the conflicting paths with `main` left alone."""
        main = _git(self.bare, "rev-parse", "main")
        theirs = _git(self.bare, "rev-parse", f"refs/heads/hosts/{host}")
        if subprocess.run(["git", "-C", str(self.bare), "merge-base", "--is-ancestor", theirs,
                           main], check=False).returncode == 0:
            return []
        out = subprocess.run(["git", "-C", str(self.bare), "merge-tree", "--write-tree",
                              "--name-only", "--allow-unrelated-histories", main, theirs],
                             capture_output=True, text=True, check=False)
        lines = out.stdout.splitlines()
        if out.returncode == 1:
            return lines[1:lines.index("", 1) if "" in lines[1:] else None]
        commit = _git(self.bare, "-c", "user.name=console", "-c", "user.email=c@c",
                      "commit-tree", lines[0], "-p", main, "-p", theirs, "-m", "merge")
        _git(self.bare, "update-ref", "refs/heads/main", commit, main)
        return []

    def account(self, *hosts: lg.LibrarySync) -> str:
        """Hosts in sync, then a copy of basic from OLD and the lock at NEW;
        the copy's commit."""
        for sync in hosts:
            sync.sync()
            self.merge_as_console(sync.host_id)
        copy = self.console({"bottles/basic.md": mark(BASIC_OLD, "basic", OLD)},
                            "copied basic from bot-bottle 0.1.16")
        self.console({LOCK_FILE: f'version = "{NEW}"\n'}, f"bot-bottle {NEW}")
        return copy

    def at_main(self, rel: str) -> str:
        return _git(self.bare, "show", f"main:{rel}") + "\n"


class Upstream(_Account):
    def test_a_copy_merges_the_releases_changes_cleanly(self) -> None:
        delphi, delos = self.on("delphi"), self.on("delos")
        copy = self.account(delphi, delos)
        # The copy's own edit, in the console, away from the release's.
        self.console({"bottles/basic.md": mark(BASIC_OLD, "basic", OLD) + "Mine too.\n"},
                     "edited in the console")
        synced = delphi.sync()
        self.assertTrue(synced.moved)
        proposal = synced.pushed
        self.assertEqual(proposal, _git(self.bare, "rev-parse", "refs/heads/hosts/delphi"))
        self.assertEqual(copy, _git(delphi.root, "rev-parse", f"{proposal}^"))
        self.assertEqual(mark(BASIC_NEW, "basic", NEW),
                         _git(delphi.root, "show", f"{proposal}:bottles/basic.md") + "\n")
        self.assertEqual(["bottles/basic.md"], _git(
            delphi.root, "diff", "--name-only", copy, proposal).split())
        self.assertEqual(
            f"bot-bottle {NEW}: bottle basic|bot-bottle {NEW} <bot-bottle@localhost>|"
            f"bot-bottle {NEW} <bot-bottle@localhost>",
            _git(delphi.root, "log", "-1", "--format=%s|%an <%ae>|%cn <%ce>", proposal))
        self.assertEqual(_git(delphi.root, "log", "-1", "--format=%cd|%cd", "--date=raw", copy),
                         _git(delphi.root, "log", "-1", "--format=%ad|%cd", "--date=raw",
                              proposal))
        # The host's own `main` stays the library's.
        self.assertEqual(_git(self.bare, "rev-parse", "main"),
                         _git(delphi.root, "rev-parse", "HEAD"))
        # Another host on the release proposes the very same commit.
        self.assertEqual(proposal, delos.sync().pushed)
        self.assertEqual([], self.merge_tree("delphi"))
        merged = self.at_main("bottles/basic.md")
        self.assertEqual(mark(BASIC_NEW, "basic", NEW) + "Mine too.\n", merged)
        self.assertEqual([], self.merge_tree("delos"))  # main has it already
        # Fast-forwarded here, and nothing more to propose.
        self.assertEqual(lg.Synced(moved=True), delphi.sync())
        self.assertEqual(merged, (delphi.root / "bottles/basic.md").read_text())
        self.assertEqual(lg.Synced(), delphi.sync())

    def test_an_overlap_is_a_conflict_until_resolved(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        edited = mark(BASIC_OLD, "basic", OLD).replace("a.example\n", "a.example\n    - host: "
                                                       "mine.example\n")
        self.console({"bottles/basic.md": edited}, "edited in the console")
        proposal = delphi.sync().pushed
        self.assertEqual(["bottles/basic.md"], self.merge_tree("delphi"))
        # Nothing more is proposed while it waits.
        self.assertEqual("", delphi.sync().pushed)
        self.assertEqual(proposal, delphi.branch_tip())
        # The console resolves it as the library's version: `main` holds the
        # proposal now, so it isn't proposed again.
        main = _git(self.bare, "rev-parse", "main")
        tree = _git(self.bare, "rev-parse", "main^{tree}")
        resolved = _git(self.bare, "-c", "user.name=c", "-c", "user.email=c@c", "commit-tree",
                        tree, "-p", main, "-p", proposal, "-m", "resolved")
        _git(self.bare, "update-ref", "refs/heads/main", resolved)
        self.assertEqual(lg.Synced(moved=True), delphi.sync())
        self.assertEqual(lg.Synced(), delphi.sync())

    def test_the_hosts_own_work_replaces_a_proposal_and_it_comes_back(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        edited = mark(BASIC_OLD, "basic", OLD).replace("a.example", "z.example")
        self.console({"bottles/basic.md": edited}, "edited in the console")
        proposal = delphi.sync().pushed
        (delphi.root / "bottles/delphi.md").write_text("---\n---\nchanged\n")
        record("edited on delphi", root=delphi.root)
        synced = delphi.sync()
        self.assertTrue(synced.replaced)  # forced over the proposal
        self.assertEqual(synced.pushed, _git(self.bare, "rev-parse", "refs/heads/hosts/delphi"))
        self.assertEqual(synced.pushed, delphi.branch_tip())
        self.assertEqual([], self.merge_tree("delphi"))
        again = delphi.sync()
        self.assertTrue(again.moved)
        self.assertEqual(proposal, again.pushed)  # the same commit again

    def test_only_on_the_accounts_release(self) -> None:
        behind = self.on("delphi", installed=OLD)
        self.account(behind)
        self.assertEqual(lg.Synced(moved=True), behind.sync())
        self.assertEqual(_git(behind.root, "rev-parse", "HEAD"), behind.branch_tip())
        source = self.on("delos", installed="")
        source.sync()
        self.merge_as_console("delos")
        self.assertEqual("", source.sync().pushed)

    def test_a_copy_of_a_newer_release_isnt_merged_backwards(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        self.console({"bottles/basic.md": mark(BASIC_NEW, "basic", "0.1.18")},
                     "copied basic from bot-bottle 0.1.18")
        synced = delphi.sync()
        self.assertTrue(synced.moved)
        self.assertEqual("", synced.pushed)
        self.assertEqual((), delphi.upstream_notes)

    def test_copies_that_cant_merge_say_why_and_wait(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        self.old.pop(("bottles", "basic", OLD))  # out of reach
        self.release[("agents", "claude")] = "claude new\n"
        self.release[("bottles", "gone")] = None
        self.console({
            # Written by hand, not by Copy: no commit is the copy as made.
            "agents/claude.md": mark("claude edited\n", "claude", OLD),
            # A built-in the release no longer has: the copy is the user's.
            "bottles/gone.md": mark("gone\n", "gone", OLD),
            # Already at the account's version.
            "bottles/dev.md": mark("---\n---\ndev\n", "basic", NEW),
        }, "more copies")
        self.old[("agents", "claude", OLD)] = "claude old\n"
        synced = delphi.sync()
        self.assertEqual("", synced.pushed)
        notes = delphi.upstream_notes
        self.assertEqual(2, len(notes), notes)
        self.assertIn("agents/claude.md: no commit holds it as copied from claude@0.1.16", notes[0])
        self.assertIn("bottles/basic.md: couldn't get bot-bottle 0.1.16's basic", notes[1])
        self.assertIn("offline", notes[1])
        self.old[("bottles", "basic", OLD)] = None  # the release had none
        delphi.builtins = bc.Builtins(delphi.root, self.fetch)
        delphi.sync()
        self.assertIn("bot-bottle 0.1.16 has no built-in basic", delphi.upstream_notes[1])

    def test_a_refused_proposal_only_says_so(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        _git(self.bare, "config", "receive.denyNonFastForwards", "true")
        self.console({"bottles/basic.md": mark(BASIC_OLD, "basic", OLD) + "x\n"}, "edit")
        # The first proposal is a fast-forward of nothing: it's the branch's
        # replacement that's refused.
        _git(self.bare, "update-ref", "refs/heads/hosts/delphi",
             _git(self.bare, "rev-parse", "main"))
        synced = delphi.sync()
        self.assertEqual("", synced.pushed)
        self.assertIn("couldn't propose bot-bottle 0.1.17's changes", delphi.upstream_notes[-1])

    def test_a_git_failure_building_it_is_a_note(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        with mock.patch.object(lg, "next_upstream", side_effect=lu.UpstreamError("broken")):
            self.assertEqual("", delphi.sync().pushed)
        self.assertIn("broken", delphi.upstream_notes[0])


class Status(_Account):
    def test_behind_and_notes_are_reported(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        self.old.clear()
        delphi.sync()
        link = ll.LibraryLink(lambda m: asyncio.sleep(0), make_sync=lambda: delphi,
                              root=delphi.root)
        link.enabled = True
        self.enterContext(mock.patch.object(ll, "_active", link))
        self.enterContext(mock.patch.object(ll, "pending_conflict", return_value=None))
        self.enterContext(mock.patch.object(ll, "pending_held", return_value=None))
        with mock.patch.object(ll, "installed_release", return_value=OLD):
            report = ll.report()
        self.assertEqual(NEW, report["behind"])
        self.assertEqual(list(delphi.upstream_notes), report["upstream"])
        lines = remote_cmd.library_line(report).splitlines()
        self.assertTrue(lines[0].startswith("library: in sync at"))
        self.assertIn("behind the account's bot-bottle 0.1.17", lines[1])
        self.assertIn("couldn't get bot-bottle 0.1.16's basic", lines[2])
        with mock.patch.object(ll, "installed_release", return_value=NEW):
            self.assertNotIn("behind", ll.report())


class Builtins(unittest.IsolatedAsyncioTestCase):
    async def test_sent_on_connect_for_a_release(self) -> None:
        sent: list[dict[str, Any]] = []

        async def send(msg: dict[str, Any]) -> None:
            sent.append(msg)
        link = ll.LibraryLink(send)
        with mock.patch.object(ll, "installed_release", return_value=""):
            await link.connected()
        self.assertEqual([], sent)
        with mock.patch.object(ll, "installed_release", return_value=NEW):
            await link.connected()
        self.assertEqual(("library_builtins", NEW), (sent[0]["type"], sent[0]["version"]))
        self.assertEqual(packaged("bottles", "basic"), sent[0]["files"]["bottles/basic.md"])
        self.assertIn("agents/codex.md", sent[0]["files"])

    def test_within_what_the_console_keeps(self) -> None:
        many = {f"agents/a{i:02d}.md": "x" for i in range(40)}
        many["agents/big.md"] = "x" * (ll.MAX_BUILTIN_BYTES + 1)
        many["agents/sub/x.md"] = "x"
        with mock.patch.object(ll, "installed_release", return_value=NEW), \
                mock.patch.object(ll, "packaged_files", return_value=many):
            message = ll.builtins_message() or {}
        files = message["files"]
        assert isinstance(files, dict)
        self.assertEqual(ll.MAX_BUILTINS, len(files))
        self.assertNotIn("agents/big.md", files)
        self.assertNotIn("agents/sub/x.md", files)


class Lock(_Account):
    def test_the_lock_is_tracked_never_written(self) -> None:
        delphi = self.on("delphi")
        self.account(delphi)
        delphi.sync()
        self.assertEqual(f'version = "{NEW}"\n', (delphi.root / LOCK_FILE).read_text())
        self.assertEqual([], pending_edits(root=delphi.root))
        # A hand edit of it is a hand edit like any.
        (delphi.root / LOCK_FILE).write_text('version = "9.9.9"\n')
        self.assertEqual([f" M {LOCK_FILE}"], pending_edits(root=delphi.root))
        self.assertTrue(delphi.sync().pushed)
        self.assertIn(LOCK_FILE, _git(self.bare, "show", "--name-only", "--format=",
                                      "refs/heads/hosts/delphi").split())
        # It's in a launch's copy of a commit too.
        tree = delphi.tree_at(_git(delphi.root, "rev-parse", "HEAD"))
        self.assertEqual('version = "9.9.9"\n', (tree / LOCK_FILE).read_text())

    def test_a_lock_made_by_hand_is_committed_past_the_ignore_file(self) -> None:
        delphi = self.on("delphi")
        (delphi.root / LOCK_FILE).write_text(f'version = "{NEW}"\n')
        self.assertEqual([f"?? {LOCK_FILE}"], pending_edits(root=delphi.root))
        record("edited by hand", root=delphi.root)
        self.assertIn(LOCK_FILE, _git(delphi.root, "ls-files").split())
        (delphi.root / LOCK_FILE).unlink()
        record("edited by hand", root=delphi.root)
        self.assertNotIn(LOCK_FILE, _git(delphi.root, "ls-files").split())


if __name__ == "__main__":
    unittest.main()
