"""Projects (PRD 0131 chunk 7): the checkouts a host keeps and reports, and a
checkout's bottles at a branch for the console. Real git checkouts throughout."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import COMMANDS
from bot_bottle.cli.commands import project as project_cmd
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.control.hostctl import (checkouts, host_db, library_link,
                                        project_bottles as pb, project_link, remote)
from bot_bottle.control.hostctl.checkouts import (CheckoutStore, Found, Refused,
                                                  normalize_repo_url)
from bot_bottle.control.hostctl.project_link import ProjectLink
from bot_bottle.manifest import versions
from bot_bottle.paths import host_db_path
from tests.unit import as_cli, serve_host_controller, use_bottle_root

BOTTLE = "---\nextends: basic\n---\nA dev bottle.\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class TestNormalize(unittest.TestCase):
    def test_the_contracts_examples(self) -> None:
        for url in ("git@github.com:acme/app.git", "https://github.com/acme/app",
                    "https://GitHub.com/Acme/App.git/", "ssh://git@github.com:22/acme/app.git",
                    "https://user:tok@github.com/acme/app"):
            self.assertEqual("github.com/acme/app", normalize_repo_url(url), url)
        self.assertEqual("gitlab.com/grp/sub/app",
                         normalize_repo_url("https://gitlab.com/grp/sub/app.git"))
        for url in ("/srv/git/app.git", "file:///x/app", "https://example.com/app"):
            self.assertIsNone(normalize_repo_url(url), url)

    def test_other_forms(self) -> None:
        self.assertEqual("example.com/a/b", normalize_repo_url("git://example.com/a/b"))
        self.assertEqual("example.com/a/b", normalize_repo_url("git+ssh://u@example.com/a/b"))
        self.assertEqual("example.com/a/b", normalize_repo_url("ssh+git://example.com/a/b.git"))
        self.assertEqual("[::1]/a/b", normalize_repo_url("ssh://git@[::1]:22/a/b"))
        self.assertEqual("[::1]/a/b", normalize_repo_url("git@[::1]:a/b.git"))
        self.assertEqual("example.com/a/b.git",
                         normalize_repo_url("https://example.com/a/b.git.git"))
        for url in ("", "ftp://example.com/a/b", "git@example.com:/abs/a/b", "./a/b",
                    "https://example.com/a/..", "https://example.com/a/.git",
                    "https://exa mple.com/a/b", "https://[::1/a/b", "../x:a/b"):
            self.assertIsNone(normalize_repo_url(url), url)


class _Checkouts(unittest.TestCase):
    """A bare `origin` and a clone of it, under a fresh bottle root."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.tmp / "root"))
        # git looks no higher than the test's directory (another test may
        # leave a repo in /tmp).
        env = {**versions.git_env(), "GIT_CEILING_DIRECTORIES": str(self.tmp.parent)}
        for module in (checkouts, pb):
            self.enterContext(mock.patch.object(module, "git_env", return_value=env))
        self.origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        _git(self.tmp, "init", "-q", "-b", "main", str(seed))
        (seed / ".bot-bottle" / "bottles").mkdir(parents=True)
        (seed / ".bot-bottle" / "bottles" / "dev.md").write_text(BOTTLE)
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "seed")
        _git(seed, "remote", "add", "origin", str(self.origin))
        _git(seed, "push", "-q", "origin", "main")
        self.seed = seed
        self.work = self.tmp / "app"
        _git(self.tmp, "clone", "-q", str(self.origin), str(self.work))
        # The clone's origin is a forge's SSH URL; its `ssh` serves the bare repo.
        ssh = self.tmp / "fake-ssh"
        ssh.write_text(f"#!/bin/sh\nexec git-upload-pack '{self.origin}'\n")
        ssh.chmod(0o755)
        _git(self.work, "config", "core.sshCommand", str(ssh))
        _git(self.work, "remote", "set-url", "origin", "ssh://git@GitHub.com/Acme/App.git")
        self.store = CheckoutStore(host_db_path())

    def keep(self, path: Path | None = None, *, used: bool = False) -> checkouts.Checkout:
        found = checkouts.inspect(path or self.work)
        assert isinstance(found, Found), found
        return self.store.keep(str(path or self.work), found, used=used)


class TestCheckouts(_Checkouts):
    def test_inspect_reads_repo_and_branch(self) -> None:
        self.assertEqual(Found("github.com/acme/app", "main"), checkouts.inspect(self.work))
        _git(self.work, "checkout", "-q", "--detach")
        self.assertEqual(Found("github.com/acme/app", ""), checkouts.inspect(self.work))

    def test_inspect_refuses_what_isnt_a_checkout_with_a_repo(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIn("isn't in a git work tree", checkouts.inspect(plain).reason)  # type: ignore[union-attr]
        self.assertIn("isn't a directory", checkouts.inspect(self.tmp / "nope").reason)  # type: ignore[union-attr]
        _git(self.work, "remote", "set-url", "origin", "/srv/git/app.git")
        self.assertIn("names no forge repo", checkouts.inspect(self.work).reason)  # type: ignore[union-attr]
        _git(self.work, "remote", "remove", "origin")
        self.assertIn("no `origin` remote", checkouts.inspect(self.work).reason)  # type: ignore[union-attr]

    def test_the_only_remote_stands_in_for_origin(self) -> None:
        _git(self.work, "remote", "rename", "origin", "upstream")
        self.assertEqual("upstream", checkouts.remote_name(self.work))
        self.assertIsInstance(checkouts.inspect(self.work), Found)
        _git(self.work, "remote", "add", "other", "https://example.com/o/r")
        self.assertIsInstance(checkouts.inspect(self.work), Refused)

    def test_keep_adds_once_and_marks_launches(self) -> None:
        first = self.keep()
        self.assertIsNone(first.last_used_at)
        used = self.keep(used=True)
        self.assertEqual(first.added_at, used.added_at)
        self.assertIsNotNone(used.last_used_at)
        again = self.keep()
        self.assertEqual(used.last_used_at, again.last_used_at)
        self.assertEqual([str(self.work)], [c.path for c in self.store.all()])

    def test_a_checkout_removed_from_disk_drops_off(self) -> None:
        self.keep()
        self.assertFalse(checkouts.refresh(self.store))
        shutil.rmtree(self.work)
        self.assertTrue(checkouts.refresh(self.store))
        self.assertEqual([], self.store.all())

    def test_refresh_sees_a_new_branch_and_drops_a_lost_repo(self) -> None:
        self.keep()
        _git(self.work, "checkout", "-qb", "feat/x")
        self.assertTrue(checkouts.refresh(self.store))
        self.assertEqual("feat/x", self.store.all()[0].branch)
        _git(self.work, "remote", "remove", "origin")
        self.assertTrue(checkouts.refresh(self.store))
        self.assertEqual([], self.store.all())

    def test_refresh_leaves_one_git_cant_read(self) -> None:
        self.keep()
        with mock.patch.object(checkouts, "inspect", side_effect=OSError("no git")):
            self.assertFalse(checkouts.refresh(self.store))
        self.assertEqual(1, len(self.store.all()))

    def test_report_is_the_full_state_within_the_caps(self) -> None:
        self.assertEqual({"type": "projects", "checkouts": []}, checkouts.report(self.store))
        self.keep(used=True)
        self.store.keep("relative/path", Found("github.com/acme/app", "main"))
        self.store.keep("/x", Found("not a repo", "main"))
        self.store.keep("/y", Found("github.com/acme/app", "b" * 256))
        (row,) = checkouts.report(self.store)["checkouts"]  # type: ignore[misc]
        self.assertEqual(str(self.work), row["path"])
        self.assertEqual("github.com/acme/app", row["repo"])
        self.assertRegex(row["added"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(row["last_used"], r"Z$")
        with mock.patch.object(checkouts, "MAX_CHECKOUTS", 0):
            self.assertEqual([], checkouts.report(self.store)["checkouts"])

    def test_learn_launch_keeps_a_project_with_a_repo(self) -> None:
        checkouts.learn_launch(str(self.work / "."))
        (kept,) = self.store.all()
        self.assertEqual(str(self.work), kept.path)
        self.assertIsNotNone(kept.last_used_at)

    def test_learn_launch_skips_a_non_project_or_no_repo(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        checkouts.learn_launch(str(plain))
        _git(self.work, "remote", "remove", "origin")
        checkouts.learn_launch(str(self.work))
        with mock.patch.object(checkouts, "inspect", side_effect=OSError("boom")):
            checkouts.learn_launch(str(self.seed))  # no remote either way; never raises
        self.assertEqual([], self.store.all())

    def test_a_project_launch_keeps_its_checkout(self) -> None:
        with mock.patch.object(library_link, "launch_versions",
                               return_value={"project": "abc"}), \
                mock.patch.object(library_link, "learn_launch") as learn:
            library_link.launch_index(LaunchRequest(agent="a"), str(self.work),
                                      lambda cwd: mock.Mock())
        learn.assert_called_once_with(str(self.work))
        with mock.patch.object(library_link, "launch_versions", return_value={}), \
                mock.patch.object(library_link, "learn_launch") as learn:
            library_link.launch_index(LaunchRequest(agent="a"), str(self.work),
                                      lambda cwd: mock.Mock())
        learn.assert_not_called()

    def test_the_host_db_owns_the_table(self) -> None:
        self.assertIn("project_checkouts",
                      [t for s in host_db.stores(host_db_path()) for t in s.TABLES])


class TestRoutes(_Checkouts):
    def test_add_list_remove(self) -> None:
        reply = checkouts.add({"path": str(self.work)})
        self.assertEqual("github.com/acme/app", reply["checkout"]["repo"])  # type: ignore[index]
        self.assertEqual("", reply["note"])
        listed = checkouts.listed({})["checkouts"]
        self.assertEqual([str(self.work)], [c["path"] for c in listed])  # type: ignore[union-attr]
        self.assertEqual({"removed": True}, checkouts.remove({"path": str(self.work)}))
        self.assertEqual({"removed": False}, checkouts.remove({"path": str(self.work)}))

    def test_add_without_bottles_notes_it(self) -> None:
        sub = self.work / "sub"
        sub.mkdir()
        self.assertIn("no bottles there yet", str(checkouts.add({"path": str(sub)})["note"]))

    def test_add_refuses_saying_why(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(ValueError, "isn't in a git work tree"):
            checkouts.add({"path": str(plain)})
        with self.assertRaisesRegex(ValueError, "absolute"):
            checkouts.add({"path": "rel"})
        with mock.patch.object(checkouts, "inspect", side_effect=OSError("no git")), \
                self.assertRaisesRegex(ValueError, "can't run git"):
            checkouts.add({"path": str(self.work)})


class TestCli(_Checkouts):
    def setUp(self) -> None:
        super().setUp()
        as_cli(self)
        serve_host_controller(self)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = project_cmd.cmd_project(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_add_list_remove_over_the_host_controller(self) -> None:
        self.assertIn("project", COMMANDS)
        code, _, err = self.run_cli("list")
        self.assertEqual(0, code)
        self.assertIn("no checkouts kept", err)
        code, _, err = self.run_cli("add", str(self.work))
        self.assertEqual((0, True), (code, "github.com/acme/app" in err))
        code, out, _ = self.run_cli("list")
        self.assertEqual(f"{self.work}\tgithub.com/acme/app\tmain\tnever\n", out)
        self.store.keep(str(self.work), Found("github.com/acme/app", "main"), used=True)
        self.assertNotIn("never", self.run_cli("list")[1])
        self.assertEqual(0, self.run_cli("remove", str(self.work))[0])
        code, _, err = self.run_cli("remove", str(self.work))
        self.assertEqual(1, code)
        self.assertIn("isn't a kept checkout", err)

    def test_add_defaults_to_here_and_says_why_it_refuses(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(plain)
        code, _, err = self.run_cli("add")
        self.assertEqual(1, code)
        self.assertIn("isn't in a git work tree", err)
        os.chdir(self.work / ".bot-bottle")
        self.assertIn("note: no bottles there yet", self.run_cli("add")[2])

    def test_usage(self) -> None:
        self.assertEqual(2, self.run_cli()[0])
        self.assertEqual(0, self.run_cli("--help")[0])
        self.assertEqual(2, self.run_cli("list", "x")[0])
        self.assertEqual(2, self.run_cli("frob")[0])
        self.assertEqual("", project_cmd._when(None))
        self.assertEqual("", project_cmd._when("garbage"))


class TestProjectBottles(_Checkouts):
    def setUp(self) -> None:
        super().setUp()
        self.keep()
        # A branch only the remote has, with an agent, a bottle over the cap
        # and files that aren't manifests.
        _git(self.seed, "checkout", "-qb", "feat/x")
        bottles = self.seed / ".bot-bottle" / "bottles"
        agents = self.seed / ".bot-bottle" / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text("---\nbottle: dev\n---\nReview.\n")
        (bottles / "big.md").write_text("x" * (pb.MAX_FILE_BYTES + 1))
        (bottles / "notes.txt").write_text("not a manifest")
        (bottles / "-bad.md").write_text(BOTTLE)
        (bottles / "nested").mkdir()
        (bottles / "nested" / "deep.md").write_text(BOTTLE)
        os.symlink("dev.md", bottles / "link.md")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-qm", "feat")
        _git(self.seed, "push", "-q", "origin", "feat/x")

    def ask(self, **fields: Any) -> dict[str, Any]:
        msg = {"type": "project_bottles", "request_id": "0123456789abcdef",
               "repo": "github.com/acme/app", "path": str(self.work), "branch": "", **fields}
        return pb.answer(msg, self.store)

    def test_a_branch_only_the_remote_has_after_a_fetch(self) -> None:
        head = _git(self.work, "rev-parse", "HEAD")
        reply = self.ask(branch="feat/x")
        self.assertNotIn("error", reply)
        self.assertTrue(reply["fetched"])
        self.assertEqual(_git(self.seed, "rev-parse", "HEAD"), reply["commit"])
        self.assertEqual(["big", "dev"], [b["name"] for b in reply["bottles"]])
        big, dev = reply["bottles"]
        self.assertEqual({"name": "big", "file": ".bot-bottle/bottles/big.md", "content": None,
                          "too_large": True}, big)
        self.assertEqual({"name": "dev", "file": ".bot-bottle/bottles/dev.md",
                          "content": BOTTLE}, dev)
        self.assertEqual(["reviewer"], [a["name"] for a in reply["agents"]])
        self.assertEqual(["feat/x", "main"], reply["branches"])
        self.assertEqual("0123456789abcdef", reply["request_id"])
        # Nothing checked out: the working tree and its branch are as they were.
        self.assertEqual(head, _git(self.work, "rev-parse", "HEAD"))
        self.assertEqual("main", _git(self.work, "symbolic-ref", "--short", "HEAD"))
        self.assertEqual("", _git(self.work, "status", "--porcelain"))
        self.assertFalse((self.work / ".bot-bottle" / "agents").exists())

    def test_head_and_a_local_branch(self) -> None:
        reply = self.ask()
        self.assertEqual(_git(self.work, "rev-parse", "HEAD"), reply["commit"])
        self.assertEqual(["dev"], [b["name"] for b in reply["bottles"]])
        self.assertEqual([], reply["agents"])
        self.assertEqual("main", self.ask(branch="main")["branch"])

    def test_a_project_in_a_subdirectory(self) -> None:
        sub = self.seed / "svc" / ".bot-bottle" / "bottles"
        sub.mkdir(parents=True)
        (sub / "api.md").write_text(BOTTLE)
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-qm", "svc")
        _git(self.seed, "push", "-q", "origin", "feat/x")
        (self.work / "svc").mkdir()
        self.keep(self.work / "svc")
        reply = self.ask(path=str(self.work / "svc"), branch="feat/x")
        self.assertEqual([{"name": "api", "file": "svc/.bot-bottle/bottles/api.md",
                           "content": BOTTLE}], reply["bottles"])

    def test_a_checkout_it_didnt_report_is_refused(self) -> None:
        other = self.tmp / "other"
        _git(self.tmp, "clone", "-q", str(self.origin), str(other))
        for fields in ({"path": str(other)}, {"repo": "github.com/acme/other"},
                       {"path": str(self.work) + "/"}, {"path": ""}, {"repo": None}):
            reply = self.ask(**fields)
            self.assertEqual({"type", "request_id", "error"}, set(reply), fields)
        self.assertIn("this host has no checkout of github.com/acme/app at",
                      self.ask(path=str(other))["error"])

    def test_bad_and_missing_branches(self) -> None:
        self.assertIn("isn't a branch name", self.ask(branch="-x")["error"])
        self.assertIn("isn't a branch name", self.ask(branch="a..b")["error"])
        self.assertIn("isn't a branch name", self.ask(branch="@{-1}")["error"])
        self.assertIn("no branch nope in", self.ask(branch="nope")["error"])

    def test_a_failed_fetch_carries_on(self) -> None:
        _git(self.work, "config", "core.sshCommand", "false")
        reply = self.ask(branch="main")
        self.assertFalse(reply["fetched"])
        self.assertEqual(["dev"], [b["name"] for b in reply["bottles"]])

    def test_a_checkout_gone_since_it_was_reported(self) -> None:
        shutil.rmtree(self.work)
        self.assertIn("is gone from this host", self.ask()["error"])

    def test_a_fetch_that_hangs_is_killed(self) -> None:
        self.assertFalse(pb.fetch(self.work, "origin", timeout=0.0))
        with mock.patch.object(pb.subprocess, "Popen", side_effect=OSError("no git")):
            self.assertFalse(pb.fetch(self.work, "origin"))

    def test_the_caps(self) -> None:
        with mock.patch.object(pb, "MAX_FILES", 1), mock.patch.object(pb, "MAX_BRANCHES", 1):
            reply = self.ask(branch="feat/x")
        self.assertEqual(["big"], [b["name"] for b in reply["bottles"]])
        self.assertEqual(["feat/x"], reply["branches"])

    def test_git_failing_is_an_error_reply(self) -> None:
        with mock.patch.object(pb, "manifests", side_effect=pb._Refused("couldn't list x")):
            self.assertEqual("couldn't list x", self.ask()["error"])
        with mock.patch.object(pb, "git", side_effect=OSError("no git")):
            self.assertIn("couldn't read the project: no git", self.ask(branch="x")["error"])
        commit = _git(self.work, "rev-parse", "HEAD")
        with self.assertRaises(pb._Refused):
            pb.manifests(self.work, "0" * 40, ".bot-bottle/bottles/")
        with self.assertRaises(pb._Refused):
            pb._show(self.work, commit, "nope.md")


class _Sent:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class TestProjectLink(_Checkouts):
    def test_connect_reports_and_a_change_reports_again(self) -> None:
        sent = _Sent()
        link = ProjectLink(sent, store=lambda: self.store)

        async def run() -> None:
            await link.connected()
            await link.tick()  # nothing changed: nothing sent
            self.keep()
            await link.tick()
            shutil.rmtree(self.work)
            await link.tick(refresh=True)
            await link.connected()  # a reconnect hears it all again

        asyncio.run(run())
        self.assertEqual([[], [str(self.work)], [], []],
                         [[c["path"] for c in m["checkouts"]] for m in sent.messages])
        self.assertTrue(all(m["type"] == "projects" for m in sent.messages))

    def test_answers_project_bottles_beside_the_link(self) -> None:
        self.keep()
        sent = _Sent()
        link = ProjectLink(sent, store=lambda: self.store)

        async def run() -> None:
            await link.handle({"type": "project_bottles", "request_id": "r",
                               "repo": "github.com/acme/app", "path": str(self.work),
                               "branch": ""})
            await asyncio.gather(*link._asked)
            link.close()

        asyncio.run(run())
        self.assertEqual(1, len(sent.messages))
        reply = sent.messages[0]
        self.assertEqual(("project_bottles", "r"), (reply["type"], reply["request_id"]))
        self.assertEqual(["dev"], [b["name"] for b in reply["bottles"]])

    def test_a_store_that_cant_be_read(self) -> None:
        sent = _Sent()

        def broken() -> CheckoutStore:
            raise OSError("no db")

        link = ProjectLink(sent, store=broken)

        async def run() -> None:
            await link.tick()
            await link.handle({"type": "project_bottles", "request_id": "r"})
            await asyncio.gather(*link._asked)

        asyncio.run(run())
        self.assertEqual([{"type": "project_bottles", "request_id": "r",
                           "error": "couldn't read this host's checkouts: no db"}],
                         sent.messages)

    def test_the_console_link_routes_and_reports_projects(self) -> None:
        sent = _Sent()
        bottles = mock.Mock()
        bottles.list.return_value = []
        link = remote.ConsoleLink(bottles, sent, agents=lambda: [])
        link.projects = ProjectLink(sent, store=lambda: self.store)
        self.keep()

        async def run() -> None:
            await link.projects.connected()
            await link.handle({"type": "project_bottles", "request_id": "r",
                               "repo": "github.com/acme/app", "path": "/elsewhere",
                               "branch": ""})
            await asyncio.gather(*link.projects._asked)
            link.close()

        asyncio.run(run())
        self.assertEqual(["projects", "project_bottles"], [m["type"] for m in sent.messages])
        self.assertIn("error", sent.messages[1])
        self.assertEqual(60.0, project_link.REFRESH_SECONDS)


if __name__ == "__main__":
    unittest.main()
