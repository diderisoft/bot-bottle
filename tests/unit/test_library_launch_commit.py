"""Unit: console launches at a library commit (PRD 0131). The host resolves
library bottles and agents from the commit the console names, fetching the
library's `main` for it when missing, and never touches its working tree or
`main`. Real git repos throughout; a local bare repo stands in for the
library app."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import run_manifest
from bot_bottle.contract.auth import ROLE_CLI
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.control.hostctl import library_link as ll
from bot_bottle.control.hostctl import pairing
from bot_bottle.control.hostctl.remote_requests import agent_allowed, library_commit
from bot_bottle.manifest import ManifestBottle, ManifestIndex
from bot_bottle.manifest.versions import launch_versions, record

_AGENT = "---\nagent_provider:\n  harness: claude\n---\nYou are {}.\n"
_BOTTLE = "---\nenv:\n  WHO: {}\n---\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class _Hosts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.bare = self.tmp / "library.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        self.tokens = 0

    def token(self) -> ll.Token:
        self.tokens += 1
        return ll.Token(url=str(self.bare), username="host", token="t",
                        expires=time.time() + 900)

    def host(self, name: str, who: str) -> ll.LibrarySync:
        root = self.tmp / name / ".bot-bottle"
        (root / "bottles").mkdir(parents=True)
        (root / "agents").mkdir()
        (root / "agents/dev.md").write_text(_AGENT.format(who))
        (root / "bottles/work.md").write_text(_BOTTLE.format(who))
        record(f"write on {name}", root=root)
        return ll.LibrarySync(host_id=name, token_source=self.token, root=root)

    def publish(self, sync: ll.LibrarySync) -> str:
        """`sync`'s `main` becomes the library's (as the console's merge would)."""
        _git(sync.root, "push", "-q", str(self.bare), "HEAD:refs/heads/main")
        return _git(sync.root, "rev-parse", "HEAD")

    def edit(self, sync: ll.LibrarySync, who: str, *, commit: bool = True) -> None:
        (sync.root / "bottles/work.md").write_text(_BOTTLE.format(who))
        if commit:
            record(f"edit to {who}", root=sync.root)


class TreeAt(_Hosts):
    def test_a_commit_the_host_has_resolves_without_touching_its_copy(self) -> None:
        delphi = self.host("delphi", "first")
        sha = self.publish(delphi)
        self.edit(delphi, "second")
        self.edit(delphi, "unsaved", commit=False)
        head = _git(delphi.root, "rev-parse", "HEAD")
        tree = delphi.tree_at(sha)
        self.assertEqual(_BOTTLE.format("first"), (tree / "bottles/work.md").read_text())
        self.assertEqual(_AGENT.format("first"), (tree / "agents/dev.md").read_text())
        self.assertEqual(head, _git(delphi.root, "rev-parse", "HEAD"))
        self.assertEqual(_BOTTLE.format("unsaved"),
                         (delphi.root / "bottles/work.md").read_text())
        self.assertEqual(0, self.tokens)  # present: no fetch, no token
        # Kept out of the repo's tracked files.
        self.assertNotIn(ll.TREES_DIR, _git(delphi.root, "status", "--porcelain"))
        self.assertEqual(0o700, (delphi.root / ll.TREES_DIR).stat().st_mode & 0o777)

    def test_a_missing_commit_is_fetched_from_the_library(self) -> None:
        delos = self.host("delos", "delos")
        sha = self.publish(delos)
        delphi = self.host("delphi", "delphi")
        head = _git(delphi.root, "rev-parse", "HEAD")
        tree = delphi.tree_at(sha)
        self.assertEqual(_BOTTLE.format("delos"), (tree / "bottles/work.md").read_text())
        self.assertEqual(1, self.tokens)
        # Only the remote ref moved: `main` and the files are delphi's still.
        self.assertEqual(head, _git(delphi.root, "rev-parse", "HEAD"))
        self.assertEqual(sha, _git(delphi.root, "rev-parse", ll.REMOTE_REF))
        self.assertEqual(_BOTTLE.format("delphi"),
                         (delphi.root / "bottles/work.md").read_text())

    def test_a_commit_the_library_lacks_fails_clearly(self) -> None:
        delphi = self.host("delphi", "delphi")
        self.publish(delphi)
        with self.assertRaises(ll.LibraryError) as caught:
            delphi.tree_at("0" * 40)
        self.assertIn("library commit 000000000000 isn't on this host", str(caught.exception))
        self.assertEqual(1, self.tokens)

    def test_a_diverged_main_still_launches_the_libraries_version(self) -> None:
        delphi = self.host("delphi", "base")
        self.publish(delphi)
        delos = self.host("delos", "x")
        _git(delos.root, "fetch", "-q", str(self.bare), "main")
        _git(delos.root, "reset", "-q", "--hard", "FETCH_HEAD")
        self.edit(delos, "theirs")
        sha = self.publish(delos)
        self.edit(delphi, "mine")  # a pending conflict: delphi's main diverged
        tree = delphi.tree_at(sha)
        self.assertEqual(_BOTTLE.format("theirs"), (tree / "bottles/work.md").read_text())
        self.assertEqual(_BOTTLE.format("mine"), (delphi.root / "bottles/work.md").read_text())
        self.assertNotEqual(sha, _git(delphi.root, "rev-parse", "HEAD"))

    def test_a_tree_is_reused_and_only_the_latest_are_kept(self) -> None:
        delphi = self.host("delphi", "v0")
        shas = [self.publish(delphi)]
        for n in range(1, 4):
            self.edit(delphi, f"v{n}")
            shas.append(self.publish(delphi))
        first = delphi.tree_at(shas[0])
        (first / "bottles/marker").write_text("")
        self.assertTrue((delphi.tree_at(shas[0]) / "bottles/marker").exists())  # reused
        with mock.patch.object(ll, "KEEP_TREES", 2), \
                mock.patch.object(ll, "TREE_GRACE_SECONDS", 0.0):
            for sha in shas[1:]:
                delphi.tree_at(sha)
        kept = sorted(p.name for p in (delphi.root / ll.TREES_DIR).iterdir())
        self.assertEqual(sorted(shas[2:]), kept)

    def test_a_tree_another_launch_made_first_is_kept(self) -> None:
        delphi = self.host("delphi", "v0")
        sha = self.publish(delphi)
        real = Path.rename

        def raced(path: Path, target: Path) -> Path:
            target.mkdir()
            (target / "theirs").write_text("")
            return real(path, target)
        with mock.patch.object(Path, "rename", raced):
            tree = delphi.tree_at(sha)
        self.assertEqual(["theirs"], [p.name for p in tree.iterdir()])
        self.assertEqual([sha], [p.name for p in (delphi.root / ll.TREES_DIR).iterdir()])

    def test_a_commit_in_no_history_is_refused(self) -> None:
        delphi = self.host("delphi", "v0")
        self.publish(delphi)
        self.edit(delphi, "dropped")
        dangling = _git(delphi.root, "rev-parse", "HEAD")
        _git(delphi.root, "reset", "-q", "--hard", "HEAD~1")  # still an object here
        with self.assertRaisesRegex(ll.LibraryError, "isn't on this host or in the account"):
            delphi.tree_at(dangling)
        self.assertEqual(1, self.tokens)  # it looked in the library's main first
        self.assertFalse((delphi.root / ll.TREES_DIR / dangling).exists())

    def test_a_failed_fetch_says_which_commit(self) -> None:
        delphi = self.host("delphi", "v0")
        delphi.token_source = lambda: ll.Token(url=str(self.tmp / "gone.git"), username="h",
                                               token="t", expires=time.time() + 900)
        with self.assertRaisesRegex(ll.LibraryError, "couldn't fetch library commit aaaaaaaaaaaa"):
            delphi.tree_at("a" * 40)

    def test_only_files_are_copied(self) -> None:
        delphi = self.host("delphi", "v0")
        os.symlink("/etc/passwd", delphi.root / "bottles/link.md")
        blob = _git(delphi.root, "hash-object", "-w", "--stdin")  # empty
        head = _git(delphi.root, "rev-parse", "HEAD")
        record("a link", root=delphi.root)
        # A tree git's own checks would refuse, made locally: a `..` entry
        # and a submodule, beside the link.
        bottles = _git(delphi.root, "rev-parse", "HEAD:bottles")
        listing = _git(delphi.root, "ls-tree", bottles) + (
            f"\n100644 blob {blob}\t..\n160000 commit {head}\tmod\n")
        odd = subprocess.run(["git", "-C", str(delphi.root), "mktree"], input=listing,
                             capture_output=True, text=True, check=True).stdout.strip()
        top = subprocess.run(["git", "-C", str(delphi.root), "mktree"],
                             input=_git(delphi.root, "ls-tree", "HEAD").replace(
                                 bottles, odd), capture_output=True, text=True,
                             check=True).stdout.strip()
        sha = _git(delphi.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree",
                   top, "-p", "HEAD", "-m", "odd")
        _git(delphi.root, "update-ref", "HEAD", sha)
        tree = delphi.tree_at(sha)
        link = tree / "bottles/link.md"
        self.assertFalse(link.is_symlink())
        self.assertEqual("/etc/passwd", link.read_text())
        self.assertEqual(["link.md", "work.md"],
                         sorted(p.name for p in (tree / "bottles").iterdir()))

    def test_a_failed_copy_leaves_nothing(self) -> None:
        delphi = self.host("delphi", "v0")
        sha = self.publish(delphi)
        with mock.patch.object(ll.LibrarySync, "_blob", side_effect=ll.LibraryError("boom")), \
                self.assertRaisesRegex(ll.LibraryError, "boom"):
            delphi.tree_at(sha)
        self.assertEqual([], list((delphi.root / ll.TREES_DIR).iterdir()))

    def test_a_copy_in_use_and_staging_left_by_a_crash(self) -> None:
        delphi = self.host("delphi", "v0")
        shas = [self.publish(delphi)]
        for n in range(1, 3):
            self.edit(delphi, f"v{n}")
            shas.append(self.publish(delphi))
        trees = delphi.root / ll.TREES_DIR
        delphi.tree_at(shas[0])
        crashed = trees / ".new-crashed"
        crashed.mkdir()
        old = time.time() - 2 * ll.TREE_GRACE_SECONDS
        with mock.patch.object(ll, "KEEP_TREES", 1):
            delphi.tree_at(shas[1])  # the first was used just now: kept
            self.assertEqual(sorted([".new-crashed", *shas[:2]]),
                             sorted(p.name for p in trees.iterdir()))
            for path in (crashed, trees / shas[0]):
                os.utime(path, (old, old))
            delphi.tree_at(shas[2])
        self.assertEqual(sorted(shas[1:]), sorted(p.name for p in trees.iterdir()))

    def test_refusals(self) -> None:
        delphi = self.host("delphi", "v0")
        with self.assertRaisesRegex(ll.LibraryError, "isn't a commit sha"):
            delphi.tree_at("HEAD")
        nowhere = ll.LibrarySync(host_id="", root=self.tmp / "nowhere")
        with self.assertRaisesRegex(ll.LibraryError, "no library repo"):
            nowhere.ensure_commit("a" * 40)

        def unpaired() -> ll.Token:
            raise pairing.RemoteError("not paired")
        lost = ll.LibrarySync(host_id="", token_source=unpaired, root=delphi.root)
        with self.assertRaisesRegex(ll.LibraryError, "couldn't get a library token"):
            lost.ensure_commit("a" * 40)

    def test_an_unreadable_blob_is_an_error(self) -> None:
        delphi = self.host("delphi", "v0")
        with self.assertRaisesRegex(ll.LibraryError, "git cat-file"):
            delphi._blob("f" * 40)  # pylint: disable=protected-access


class LaunchIndex(_Hosts):
    def setUp(self) -> None:
        super().setUp()
        self.delphi = self.host("delphi", "base")
        env = mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.delphi.root)})
        env.start()
        self.addCleanup(env.stop)
        sync = mock.patch.object(ll, "_launch_sync", return_value=self.delphi)
        sync.start()
        self.addCleanup(sync.stop)

    def index(self, request: LaunchRequest) -> tuple[dict[str, object], ManifestIndex]:
        return ll.launch_index(request, str(self.tmp),
                               lambda _cwd: ManifestIndex.from_md_dirs(self.delphi.root, None))

    def test_a_named_commit_resolves_library_bottles_and_agents_there(self) -> None:
        sha = self.publish(self.delphi)
        (self.delphi.root / "agents/dev.md").write_text(_AGENT.format("later"))
        self.edit(self.delphi, "later", commit=False)  # a hand edit, not versioned
        head = _git(self.delphi.root, "rev-parse", "HEAD")
        versions, index = self.index(LaunchRequest(agent="dev", bottles=("work",),
                                                   library_commit=sha, run_bottle=""))
        self.assertEqual(sha, versions["host"])
        manifest = index.load_for_agent("dev", ("work",))
        self.assertEqual({"WHO": "base"}, dict(manifest.bottle.env))
        self.assertIn("You are base.", manifest.agent.prompt)
        self.assertEqual(head, _git(self.delphi.root, "rev-parse", "HEAD"))  # no hand-edit commit
        self.assertEqual(self.delphi.root / ll.TREES_DIR / sha,
                         ll.commit_tree(LaunchRequest(agent="dev", library_commit=sha)))

    def test_names_the_commit_lacks_resolve_from_the_hosts_copy(self) -> None:
        sha = self.publish(self.delphi)
        (self.delphi.root / "agents/mine.md").write_text(_AGENT.format("mine"))
        (self.delphi.root / "bottles/mine.md").write_text(_BOTTLE.format("mine"))
        self.edit(self.delphi, "later")
        _versions, index = self.index(LaunchRequest(agent="dev", library_commit=sha))
        index.require_agent("mine")
        self.assertIn("You are mine.", index.load_for_agent("mine", ("mine",)).agent.prompt)
        self.assertEqual({"WHO": "base"}, dict(index.load_for_agent("dev", ("work",)).bottle.env))
        tree = self.delphi.root / ll.TREES_DIR / sha
        self.assertEqual(tree / "agents/dev.md", index.host_agent("dev"))
        self.assertEqual(self.delphi.root / "agents/mine.md", index.host_agent("mine"))
        self.assertIsNone(index.host_agent("nobody"))
        self.assertIn("mine", index.all_agent_names)

    def test_without_a_commit_nothing_changes(self) -> None:
        self.edit(self.delphi, "hand", commit=False)
        versions, index = self.index(LaunchRequest(agent="dev", run_bottle="---\n---\n",
                                                   bottles=("suggested",)))
        self.assertEqual(_git(self.delphi.root, "rev-parse", "HEAD"), versions["host"])
        self.assertIn("edited by hand", _git(self.delphi.root, "log", "-1", "--format=%s"))
        self.assertEqual("---\n---\n", versions["run_bottle"])
        self.assertEqual(self.delphi.root, index.home_md)
        self.assertIsNone(ll.commit_tree(LaunchRequest(agent="dev")))

    def test_the_launch_fingerprint_is_taken_at_the_commit(self) -> None:
        sha = self.publish(self.delphi)
        self.edit(self.delphi, "later")
        with mock.patch("bot_bottle.host_access.in_host_controller", return_value=True), \
                mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            run_manifest.record_launch("s1", ManifestBottle(), names=("work",),
                                       cwd=str(self.tmp), host_version=sha,
                                       library=self.delphi.tree_at(sha))
            at_commit = run_manifest.launch_record("s1")["bottles"]["work"]
            run_manifest.record_launch("s2", ManifestBottle(), names=("work",),
                                       cwd=str(self.tmp))
            now = run_manifest.launch_record("s2")["bottles"]["work"]
        self.assertNotEqual(at_commit, now)


class LaunchSync(unittest.TestCase):
    def test_the_active_links_sync_else_one_for_the_pairing(self) -> None:
        self.assertIsNone(ll._active)  # pylint: disable=protected-access
        self.assertEqual("", ll._launch_sync().host_id)  # pylint: disable=protected-access
        sync = ll.LibrarySync(host_id="delphi")
        link = ll.LibraryLink(mock.AsyncMock(), make_sync=lambda: sync)
        with mock.patch.object(ll, "_active", link):
            self.assertIs(sync, ll._launch_sync())  # pylint: disable=protected-access


class CommitTree(unittest.TestCase):
    def test_unpaired_since_the_launch_is_the_default_root(self) -> None:
        link = ll.LibraryLink(mock.AsyncMock())
        with mock.patch.object(ll, "_active", link), \
                mock.patch.object(pairing, "load_pairing",
                                  side_effect=pairing.RemoteError("not paired")), \
                mock.patch.object(ll, "bot_bottle_root", return_value=Path("/r")):
            self.assertEqual(Path("/r") / ll.TREES_DIR / ("a" * 40),
                             ll.commit_tree(LaunchRequest(agent="dev", library_commit="a" * 40)))


class Contract(unittest.TestCase):
    def test_a_launch_names_a_full_commit_sha_or_none(self) -> None:
        sha = "ab" * 20
        request = LaunchRequest.from_json({"agent": "dev", "library_commit": sha}, ROLE_CLI)
        self.assertEqual(sha, request.library_commit)
        self.assertEqual(sha, LaunchRequest.from_json(request.to_json(), ROLE_CLI).library_commit)
        for bad in ("abc123", "AB" * 20, "g" * 40, "a" * 41):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "library_commit"):
                LaunchRequest.from_json({"agent": "dev", "library_commit": bad}, ROLE_CLI)

    def test_the_console_messages_commit(self) -> None:
        sha = "0123456789abcdef" * 2 + "01234567"
        self.assertEqual("", library_commit({}))
        self.assertEqual("", library_commit({"library_commit": None}))
        self.assertEqual(sha, library_commit({"library_commit": sha}))
        for bad in (5, "HEAD", sha.upper(), sha + "\n", ["x"]):
            with self.subTest(bad=bad):
                self.assertIsNone(library_commit({"library_commit": bad}))

    def test_an_agent_named_at_a_commit_is_checked_there(self) -> None:
        mine = lambda: ["dev"]  # noqa: E731  # pylint: disable=unnecessary-lambda-assignment
        self.assertTrue(agent_allowed("dev", mine, ""))
        self.assertFalse(agent_allowed("other", mine, ""))
        self.assertTrue(agent_allowed("other", mine, "a" * 40))
        self.assertFalse(agent_allowed("../x", mine, "a" * 40))
        self.assertFalse(agent_allowed(None, mine, "a" * 40))


class Versions(unittest.TestCase):
    def test_a_named_commit_is_the_host_version_and_nothing_is_committed(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / ".bot-bottle"  # pylint: disable=consider-using-with
        (root / "bottles").mkdir(parents=True)
        (root / "bottles/a.md").write_text("a\n")
        self.assertEqual({"host": "c" * 40},
                         launch_versions(str(root.parent), root=root, host="c" * 40))
        self.assertFalse((root / ".git").exists())


if __name__ == "__main__":
    unittest.main()
