"""Launching a project from the console (PRD 0131 chunk 8): the worktree at
the commit shown, its lifecycle (teardown, failure, restart, resume), and
trust asked in the console. Real git checkouts throughout."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle import host_access
from bot_bottle.bottle_state import BottleMetadata
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.control.hostctl import (host_db, library_link, project_launch as pl,
                                        project_worktrees as wt, remote, remote_requests)
from bot_bottle.control.hostctl.project_link import ProjectLink
from bot_bottle.manifest import trust
from bot_bottle.manifest.util import ManifestError
from bot_bottle.paths import host_db_path
from tests.unit.test_projects import _Checkouts, _git, _Sent

# Grants beyond basic: needs trust.
STRIPE = """---
extends: basic
egress:
  routes:
    - host: api.stripe.com
      methods: [GET, POST]
---
"""
# A project agent spending the host's login: needs trust.
REVIEWER = "---\nagent_provider:\n  harness: claude\n  login: host\n---\nReview.\n"
REPO = "github.com/acme/app"


def only(items: list[Any]) -> Any:
    assert len(items) == 1, items
    return items[0]


class _Launches(_Checkouts):
    """A kept checkout, a home with the agent `pi`, and a link's launches
    whose `start` marks the agent running."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "root"
        for sub in ("agents", "bottles"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "agents" / "pi.md").write_text("---\n---\n")
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(mock.patch.object(host_access, "_stores", True))
        self.keep()
        self.launches = wt.ProjectLaunchStore(host_db_path())
        self.sent = _Sent()
        self.running: dict[str, str] = {}
        self.started: list[LaunchRequest] = []
        self.link = pl.ProjectLaunches(self.sent, self.start, lambda: self.running,
                                       store=lambda: self.launches, kept=lambda: self.store)

    async def start(self, agent_id: str, request: LaunchRequest) -> None:
        self.started.append(request)
        self.running[agent_id] = "bottle-1"

    def head(self) -> str:
        return _git(self.work, "rev-parse", "HEAD")

    def commit(self, files: dict[str, str], where: Path | None = None) -> str:
        where = where or self.work
        for name, text in files.items():
            (where / name).parent.mkdir(parents=True, exist_ok=True)
            (where / name).write_text(text)
        _git(where, "add", "-A")
        _git(where, "commit", "-qm", "change")
        return _git(where, "rev-parse", "HEAD")

    def msg(self, agent_id: str = "a1", **project: str) -> dict[str, Any]:
        fields = {"repo": REPO, "path": str(self.work), "branch": "main",
                  "commit": self.head(), **project}
        return {"type": "launch", "agent_id": agent_id, "definition": {"name": "pi"},
                "bottles": ["./dev"], "project": fields}

    def drive(self, *steps: Any) -> list[dict[str, Any]]:
        async def go() -> None:
            for step in steps:
                await step()
        asyncio.run(go())
        return self.sent.messages

    def worktrees(self) -> list[str]:
        out = _git(self.work, "worktree", "list", "--porcelain")
        return [line.split(" ", 1)[1] for line in out.splitlines()
                if line.startswith("worktree ")][1:]


class TestWorktree(_Launches):
    def test_runs_at_the_commit_shown_in_a_detached_locked_worktree(self) -> None:
        shown = self.head()
        self.commit({"later.txt": "x"})  # the branch moved on since the console looked
        self.drive(lambda: self.link.launch("a1", self.msg(commit=shown)))
        self.assertEqual([], self.sent.messages)
        request = only(self.started)
        tree = wt.worktree_for("a1")
        self.assertEqual(str(tree), request.cwd)
        self.assertEqual((("./dev",), "pi", False), (request.bottles, request.agent,
                                                      request.copy_cwd))
        self.assertEqual(shown, _git(tree, "rev-parse", "HEAD"))
        porcelain = _git(self.work, "worktree", "list", "--porcelain")
        self.assertIn("detached", porcelain)
        self.assertIn("locked bot-bottle: a1 (main)", porcelain)
        self.assertEqual("main", _git(self.work, "symbolic-ref", "--short", "HEAD"))
        row = self.launches.get("a1")
        assert row is not None
        self.assertEqual((wt.RUNNING, "bottle-1", str(self.work), shown),
                         (row.state, row.bottle, row.checkout, row.commit))

    def test_a_checkout_in_a_subdirectory_launches_from_its_place(self) -> None:
        sub = self.work / "svc"
        self.commit({"svc/.bot-bottle/bottles/dev.md": "---\nextends: basic\n---\n"})
        self.keep(sub)
        msg = self.msg(path=str(sub))
        self.drive(lambda: self.link.launch("a1", msg))
        self.assertEqual(str(wt.worktree_for("a1") / "svc"), self.started[0].cwd)
        self.assertEqual("svc/", self.launches.get("a1").prefix)  # type: ignore[union-attr]

    def test_teardown_removes_the_worktree_and_keeps_the_row_for_a_resume(self) -> None:
        self.drive(lambda: self.link.launch("a1", self.msg()))
        metadata = mock.Mock(spec=BottleMetadata)
        with mock.patch.object(wt, "read_metadata", return_value=metadata):
            self.drive(lambda: self.link.ended("a1"))
        self.assertFalse(wt.worktree_for("a1").exists())
        self.assertEqual([], self.worktrees())
        self.assertIsNotNone(self.launches.get("a1"))
        # The resume makes it again, at the same commit.
        cwd = str(wt.worktree_for("a1"))
        self.assertEqual(str(self.work), wt.restore(cwd, self.launches))
        self.assertEqual(self.launches.get("a1").commit,  # type: ignore[union-attr]
                         _git(wt.worktree_for("a1"), "rev-parse", "HEAD"))
        self.assertEqual(str(self.work), wt.restore(cwd, self.launches))  # already there
        # Its saved state gone, a teardown forgets it.
        self.drive(lambda: self.link.ended("a1"))
        self.assertIsNone(self.launches.get("a1"))
        self.drive(lambda: self.link.ended("a1"))  # nothing left: a no-op

    def test_a_resume_with_its_checkout_or_row_gone(self) -> None:
        self.drive(lambda: self.link.launch("a1", self.msg()))
        cwd = str(wt.worktree_for("a1"))
        self.assertIsNone(wt.restore(str(self.work), self.launches))  # not a worktree
        wt.remove(str(self.work), wt.worktree_for("a1"))
        row = self.launches.get("a1")
        assert row is not None
        self.launches.put(dataclasses.replace(row, commit="f" * 40))
        with self.assertRaisesRegex(ManifestError, "checkout is gone; start fresh"):
            wt.restore(cwd, self.launches)
        self.launches.put(dataclasses.replace(row, checkout=str(self.tmp / "nope")))
        with self.assertRaisesRegex(ManifestError, "checkout is gone"):
            wt.restore(cwd, self.launches)
        self.launches.put(row)
        with mock.patch.object(wt, "make", side_effect=wt.WorktreeError("no")), \
                self.assertRaisesRegex(ManifestError, "checkout is gone"):
            wt.restore(cwd, self.launches)
        self.launches.delete("a1")
        with self.assertRaisesRegex(ManifestError, "checkout is gone"):
            wt.restore(cwd, self.launches)

    def test_a_resume_keeps_the_checkout_never_the_worktree(self) -> None:
        self.drive(lambda: self.link.launch("a1", self.msg()))
        before = self.store.get(str(self.work))
        assert before is not None
        _git(self.work, "checkout", "-q", "-b", "other")
        shutil.rmtree(self.work / ".bot-bottle")  # the checkout has moved on
        with mock.patch.object(library_link, "launch_versions", return_value={"project": "x"}):
            library_link.launch_index(LaunchRequest(agent="pi"), str(wt.worktree_for("a1")),
                                      lambda cwd: mock.Mock())
        self.assertEqual([str(self.work)], [c.path for c in self.store.all()])
        self.assertEqual("other", self.store.get(str(self.work)).branch)  # type: ignore[union-attr]

    def test_a_failed_start_removes_the_worktree(self) -> None:
        async def fails(agent_id: str, request: LaunchRequest) -> None:
            del agent_id, request

        link = pl.ProjectLaunches(self.sent, fails, dict, store=lambda: self.launches,
                                  kept=lambda: self.store)
        self.drive(lambda: link.launch("a1", self.msg()))
        self.assertFalse(wt.worktree_for("a1").exists())
        self.assertIsNone(self.launches.get("a1"))

    def test_a_resolution_failure_ends_the_launch(self) -> None:
        msg = self.msg()
        msg["bottles"] = ["./nope"]
        self.drive(lambda: self.link.launch("a1", msg))
        errored = only(self.sent.messages)
        self.assertEqual("agent_errored", errored["type"])
        self.assertIn("launch failed: ", errored["message"])
        self.assertIn("nope", errored["message"])
        self.assertFalse(wt.worktree_for("a1").exists())
        self.assertIsNone(self.launches.get("a1"))
        self.assertEqual([], self.started)

    def test_a_worktree_git_cant_make(self) -> None:
        with mock.patch.object(wt, "git", side_effect=[
                mock.Mock(returncode=0, stdout=""),  # cat-file: the commit is there
                mock.Mock(returncode=128, stderr="fatal: no space\n")]), \
                mock.patch.object(wt, "remove"):
            self.drive(lambda: self.link.launch("a1", self.msg()))
        self.assertIn("couldn't make a worktree", self.sent.messages[0]["message"])
        self.assertIn("fatal: no space", self.sent.messages[0]["message"])
        self.assertIsNone(self.launches.get("a1"))
        with mock.patch.object(wt, "git", side_effect=OSError("no git")):
            with self.assertRaisesRegex(wt.WorktreeError, "no git"):
                wt.make(wt.Launch("a2", str(self.work), REPO, "", "f" * 40,
                                  str(wt.worktree_for("a2")), "", wt.STARTING, 0.0))
            wt.remove(str(self.work), self.tmp / "gone")  # never raises

    def test_relaunching_an_agent_replaces_its_worktree(self) -> None:
        first = self.head()
        self.drive(lambda: self.link.launch("a1", self.msg()))
        self.running.clear()
        second = self.commit({"x.txt": "x"})
        self.drive(lambda: self.link.launch("a1", self.msg(commit=second)))
        self.assertNotEqual(first, second)
        self.assertEqual(second, _git(wt.worktree_for("a1"), "rev-parse", "HEAD"))
        self.assertEqual(1, len(self.worktrees()))

    def test_the_host_db_owns_the_table(self) -> None:
        self.assertIn("project_launches",
                      [t for s in host_db.stores(host_db_path()) for t in s.TABLES])


class TestRefusals(_Launches):
    def refused(self, msg: dict[str, Any], agent_id: str = "a1") -> str:
        self.drive(lambda: self.link.launch(agent_id, msg))
        errored = only(self.sent.messages[-1:])
        self.assertEqual(("agent_errored", agent_id), (errored["type"], errored["agent_id"]))
        self.assertEqual([], self.started)
        self.assertFalse(wt.root().exists())  # nothing made
        self.assertEqual([], self.launches.all())
        return str(errored["message"])

    def test_a_checkout_it_didnt_report(self) -> None:
        self.assertEqual(f"this host has no checkout of {REPO} at /elsewhere",
                         self.refused(self.msg(path="/elsewhere")))
        self.assertEqual(f"this host has no checkout of github.com/acme/other at {self.work}",
                         self.refused(self.msg(repo="github.com/acme/other")))

    def test_bad_fields(self) -> None:
        self.assertIn("isn't a branch name", self.refused(self.msg(branch="-x")))
        self.assertIn("40-character", self.refused(self.msg(commit="abc")))
        msg = self.msg()
        msg["project"]["branch"] = 3
        self.assertIn("needs a repo, a path", self.refused(msg))
        msg = self.msg()
        msg["bottles"] = ["../dev"]
        self.assertIn("bottle names", self.refused(msg))
        msg = self.msg()
        msg["definition"] = {"name": "../x"}
        self.assertIn("isn't an agent name", self.refused(msg))
        msg["definition"] = {"name": "pi", "content": "---\n---\n"}
        self.assertIn("console-defined agent content", self.refused(msg))
        self.assertIn("console's agent id", self.refused(self.msg(), agent_id="../x"))

    def test_a_commit_it_lacks_even_after_a_fetch(self) -> None:
        self.assertEqual(f"commit {'e' * 12} isn't in {self.work}; look at the project again",
                         self.refused(self.msg(commit="e" * 40)))

    def test_an_agent_already_running(self) -> None:
        self.running["a1"] = "bottle-0"
        self.assertIn("already running", self.refused(self.msg()))

    def test_a_checkout_gone_from_git(self) -> None:
        msg = self.msg()
        shutil.rmtree(self.work / ".git")
        self.assertIn("isn't in a git work tree", self.refused(msg))

    def test_git_that_cant_run(self) -> None:
        with mock.patch.object(pl.checkouts, "git", side_effect=OSError("no git")):
            self.assertIn("couldn't read the project: no git", self.refused(self.msg()))

    def test_project_bottles_need_a_project(self) -> None:
        self.assertIsInstance(remote_requests.launch_extras({"bottles": ["./dev"]}), str)
        self.assertIsInstance(
            remote_requests.launch_extras({"bottles": ["./dev", "basic"]}, project=True), dict)
        for name in ("./dev\n", "basic\n"):  # `$` would let a trailing newline through
            self.assertIsInstance(
                remote_requests.launch_extras({"bottles": [name]}, project=True), str)
        self.assertFalse(remote_requests.agent_allowed("reviewer", lambda: [], ""))
        self.assertTrue(remote_requests.agent_allowed("reviewer", lambda: [], "", project=True))


class TestFetch(_Launches):
    def test_a_commit_missing_until_fetched(self) -> None:
        pushed = self.commit({".bot-bottle/bottles/dev.md": "---\nextends: basic\n---\nNew.\n"},
                             where=self.seed)
        _git(self.seed, "push", "-q", "origin", "main")
        self.assertFalse(wt.has_commit(self.work, pushed))
        self.drive(lambda: self.link.launch("a1", self.msg(commit=pushed)))
        self.assertEqual([], self.sent.messages)
        self.assertEqual(pushed, _git(wt.worktree_for("a1"), "rev-parse", "HEAD"))
        self.assertEqual("main", _git(self.work, "symbolic-ref", "--short", "HEAD"))


class _Trust(_Launches):
    def setUp(self) -> None:
        super().setUp()
        self.commit({".bot-bottle/bottles/dev.md": STRIPE,
                     ".bot-bottle/agents/reviewer.md": REVIEWER})

    def msg(self, agent_id: str = "a1", **project: str) -> dict[str, Any]:
        msg = super().msg(agent_id, **project)
        msg["definition"] = {"name": "reviewer"}
        return msg

    def ask(self) -> dict[str, Any]:
        self.drive(lambda: self.link.launch("a1", self.msg()))
        asked = only(self.sent.messages)
        self.sent.messages.clear()
        return asked

    def answer(self, asked: dict[str, Any], decision: str = "trust", **over: Any) -> None:
        msg = {"type": "launch_trust_answer", "agent_id": "a1", "since": asked["since"],
               "decision": decision, "hashes": [i["hash"] for i in asked["items"]], **over}
        self.drive(lambda: self.link.answer(msg))


class TestTrust(_Trust):
    def test_every_untrusted_item_is_asked_at_once(self) -> None:
        asked = self.ask()
        self.assertEqual("launch_trust", asked["type"])
        self.assertEqual((REPO, str(self.work), "main", self.head()),
                         (asked["repo"], asked["path"], asked["branch"], asked["commit"]))
        self.assertEqual([("bottle", "dev", ".bot-bottle/bottles/dev.md"),
                          ("agent", "reviewer", ".bot-bottle/agents/reviewer.md")],
                         [(i["kind"], i["name"], i["file"]) for i in asked["items"]])
        self.assertTrue(all(len(i["hash"]) == 64 and i["previous"] is None
                            for i in asked["items"]))
        self.assertIn("api.stripe.com", asked["items"][0]["grants"][0])
        self.assertEqual(["use     your claude login"], asked["items"][1]["grants"])
        self.assertEqual([], self.started)
        self.assertTrue(wt.worktree_for("a1").is_dir())  # kept while it waits
        self.assertEqual(wt.TRUST, self.launches.get("a1").state)  # type: ignore[union-attr]

    def test_trusting_records_the_checkouts_paths_and_launches(self) -> None:
        asked = self.ask()
        self.answer(asked)
        self.assertEqual([], self.sent.messages)
        self.assertEqual(1, len(self.started))
        records = {r.path: r for r in trust.load_trusted()}
        dev = records[str(self.work / ".bot-bottle" / "bottles" / "dev.md")]
        self.assertEqual(("./dev", asked["items"][0]["hash"]), (dev.bottle, dev.hash))
        self.assertEqual(tuple(asked["items"][0]["grants"]), dev.grants)
        self.assertIn(str(self.work / ".bot-bottle" / "agents" / "reviewer.md"), records)
        self.assertEqual(wt.RUNNING, self.launches.get("a1").state)  # type: ignore[union-attr]
        # Trusted in the checkout: a CLI launch there needs no prompt.
        from bot_bottle.manifest import ManifestIndex  # pylint: disable=import-outside-toplevel
        ManifestIndex.resolve(str(self.work)).load_for_agent("reviewer", ("./dev",))

    def test_a_change_since_trusted_shows_what_was(self) -> None:
        trust.trust(trust.UntrustedBottleError(
            "dev", self.work / ".bot-bottle" / "bottles" / "dev.md", digest="0" * 64,
            granted=["reach   old.example"], previous=None))
        asked = self.ask()
        previous = asked["items"][0]["previous"]
        self.assertEqual(["reach   old.example"], previous["grants"])
        self.assertRegex(previous["trusted_at"], r"^\d{4}-\d\d-\d\d$")

    def test_declining_ends_the_launch(self) -> None:
        asked = self.ask()
        self.answer(asked, "decline", hashes=[])
        errored = only(self.sent.messages)
        self.assertEqual("agent_errored", errored["type"])
        self.assertEqual(f"not started: ./dev, reviewer stay untrusted on this host "
                         f"(`bot-bottle trust ./dev` in {self.work} to review it)",
                         errored["message"])
        self.assertFalse(wt.worktree_for("a1").exists())
        self.assertIsNone(self.launches.get("a1"))
        self.assertEqual([], trust.load_trusted())
        self.assertEqual([], self.started)

    def test_one_item_stays(self) -> None:
        launch = wt.Launch("a1", str(self.work), REPO, "", "f" * 40, "/w", "", wt.TRUST, 0.0,
                           pending=json.dumps({"records": [{"kind": "agent", "name": "r"}]}))
        self.assertTrue(pl.declined(launch).startswith("not started: r stays untrusted"))

    def test_a_stale_answer_is_ignored(self) -> None:
        asked = self.ask()
        self.answer(asked, since="2020-01-01T00:00:00+00:00")
        self.answer(asked, hashes=asked["items"][0:1] and [asked["items"][0]["hash"]])
        self.answer(asked, hashes="nope")
        self.answer(asked, "maybe")
        self.answer(asked, agent_id="other")
        self.assertEqual([], self.sent.messages)
        self.assertEqual([], self.started)
        self.assertEqual([], trust.load_trusted())
        self.assertEqual(wt.TRUST, self.launches.get("a1").state)  # type: ignore[union-attr]
        # Answered, a repeat of the same answer is stale too.
        self.answer(asked)
        self.answer(asked)
        self.assertEqual(1, len(self.started))

    def test_resent_on_reconnect_with_the_same_since(self) -> None:
        asked = self.ask()
        self.drive(self.link.connected)
        self.assertEqual([asked], self.sent.messages)

    def test_unanswered_for_30_minutes(self) -> None:
        self.ask()
        self.drive(self.link.tick)
        self.assertEqual([], self.sent.messages)
        later = self.launches.get("a1")
        assert later is not None
        with mock.patch.object(pl.time, "time", return_value=time_of(later.since) + 30 * 60):
            self.drive(self.link.tick)
        self.assertEqual([{"type": "agent_errored", "agent_id": "a1",
                           "message": "not started: trust wasn't answered in 30 minutes"}],
                         self.sent.messages)
        self.assertFalse(wt.worktree_for("a1").exists())
        self.assertIsNone(self.launches.get("a1"))
        self.assertEqual(30 * 60, pl.TRUST_SECONDS)

    def test_a_reconnect_after_30_minutes_ends_it(self) -> None:
        self.ask()
        row = self.launches.get("a1")
        assert row is not None
        self.launches.put(dataclasses.replace(row, since="garbled"))
        self.drive(self.link.connected)
        self.assertEqual(["agent_errored"], [m["type"] for m in self.sent.messages])

    def test_asked_again_if_still_untrusted(self) -> None:
        asked = self.ask()
        with mock.patch.object(pl, "record"):  # recorded nothing, say
            self.answer(asked)
        again = only(self.sent.messages)
        self.assertEqual("launch_trust", again["type"])
        self.assertNotEqual(asked["since"], again["since"])
        self.assertEqual(asked["items"], again["items"])

    def test_a_trust_store_that_cant_be_written(self) -> None:
        asked = self.ask()
        with mock.patch.object(trust, "trust", side_effect=OSError("read-only")):
            self.answer(asked)
        self.assertEqual("couldn't record the trust: read-only", self.sent.messages[0]["message"])
        self.assertIsNone(self.launches.get("a1"))

    def test_too_many_untrusted_items(self) -> None:
        with mock.patch.object(pl, "MAX_ITEMS", 1):
            self.drive(lambda: self.link.launch("a1", self.msg()))
        self.assertEqual("agent_errored", self.sent.messages[0]["type"])
        self.assertIn("isn't trusted", self.sent.messages[0]["message"])


def time_of(iso: str) -> float:
    from datetime import datetime  # pylint: disable=import-outside-toplevel
    return datetime.fromisoformat(iso).timestamp()


class TestSweep(_Trust):
    def test_on_start_only_running_bottles_keep_their_worktrees(self) -> None:
        self.ask()  # a1 waits on trust
        self.drive(lambda: self.link.launch("a2", {**self.msg(), "definition": {"name": "pi"},
                                                 "bottles": []}))  # a2 runs
        self.running.clear()
        self.drive(lambda: self.link.launch("a3", {**self.msg(), "definition": {"name": "pi"},
                                                 "bottles": []}))  # a3 ran, now stopped
        row = self.launches.get("a3")
        assert row is not None
        self.launches.put(dataclasses.replace(row, bottle="bottle-3"))
        self.launches.put(dataclasses.replace(row, agent_id="a4", state=wt.STARTING,
                                              worktree=str(wt.worktree_for("a4"))))
        (wt.root() / "stray").mkdir()
        wt.sweep({"bottle-1"}, self.launches)
        self.assertEqual(["a2"], sorted(p.name for p in wt.root().iterdir()))
        states = {r.agent_id: r.state for r in self.launches.all()}
        self.assertEqual({"a1": wt.ERRORED, "a2": wt.RUNNING, "a4": wt.ERRORED}, states)
        self.assertEqual(1, len(self.worktrees()))
        self.sent.messages.clear()
        self.drive(self.link.connected)
        self.assertEqual([("a1", wt.INTERRUPTED), ("a4", wt.INTERRUPTED)],
                         sorted((m["agent_id"], m["message"]) for m in self.sent.messages))
        self.assertEqual(["a2"], [r.agent_id for r in self.launches.all()])

    def test_a_sweep_that_cant_read(self) -> None:
        broken = mock.Mock()
        broken.all.side_effect = OSError("no db")
        wt.sweep(set(), broken)  # logged, never raises
        wt.sweep(set())  # the host's own store, and no worktrees yet


class TestConsoleLink(_Trust):
    def link_with(self, bottles: mock.Mock) -> remote.ConsoleLink:
        link = remote.ConsoleLink(bottles, self.sent, agents=lambda: [])
        link.projects = ProjectLink(self.sent, store=lambda: self.store, start=link._start,
                                    running=lambda: link.running)
        link.projects.launches._store = lambda: self.launches
        return link

    def test_launch_trust_and_teardown_over_the_link(self) -> None:
        bottles = mock.Mock()
        bottles.launch.return_value = {"bottle": "bottle-1"}
        bottles.list.return_value = []
        bottles.teardown.return_value = {"exit": {"status": 0}}
        link = self.link_with(bottles)

        async def go() -> None:
            await link.handle(self.msg())
            asked = self.sent.messages[-1]
            await link.handle({"type": "launch_trust_answer", "agent_id": "a1",
                               "since": asked["since"], "decision": "trust",
                               "hashes": [i["hash"] for i in asked["items"]]})
            await link.handle({"type": "stop", "agent_id": "a1"})

        asyncio.run(go())
        self.assertEqual(["launch_trust", "agent_started", "agent_stopped"],
                         [m["type"] for m in self.sent.messages])
        request = bottles.launch.call_args.args[0]
        self.assertEqual((str(wt.worktree_for("a1")), "reviewer", ("./dev",)),
                         (request.cwd, request.agent, request.bottles))
        self.assertFalse(wt.worktree_for("a1").exists())
        self.assertIn("launch_trust_answer", remote.ConsoleLink.SLOW)

    def test_an_agent_that_exits_loses_its_worktree(self) -> None:
        bottles = mock.Mock()
        bottles.session_state.return_value = {"running": False, "exit": {"status": 0}}
        link = self.link_with(bottles)
        launch = wt.Launch("a9", str(self.work), REPO, "", self.head(),
                           str(wt.worktree_for("a9")), "", wt.RUNNING, 0.0, bottle="bottle-9")
        self.launches.put(launch)
        wt.make(launch)
        link.running["a9"] = "bottle-9"
        asyncio.run(link.reap())
        self.assertFalse(wt.worktree_for("a9").exists())
        self.assertEqual("agent_stopped", self.sent.messages[-1]["type"])

    def test_a_console_resume_of_a_project_agent(self) -> None:
        metadata = mock.Mock(agent_name="reviewer", bottle_names=["./dev"], label="l",
                             cwd=str(wt.worktree_for("a1")), copy_cwd=False)
        with mock.patch("bot_bottle.bottle_state.read_metadata", return_value=metadata):
            request = remote_requests.resume_request(
                "a1", "bottle-1", agents=lambda: [], running=lambda b: False)
        assert isinstance(request, LaunchRequest)
        self.assertEqual(("reviewer", str(wt.worktree_for("a1"))), (request.agent, request.cwd))

    def test_a_store_that_cant_be_read(self) -> None:
        link = ProjectLink(self.sent, store=lambda: self.store)
        broken = mock.Mock()
        broken.all.side_effect = OSError("no db")
        link.launches._store = lambda: broken

        async def go() -> None:
            await link.connected()
            await link.tick()
            await link.launches.ended("a1")
            with self.assertRaises(RuntimeError):
                await link.launches._start("a1", LaunchRequest(agent="pi"))

        asyncio.run(go())
        self.assertEqual(["projects"], [m["type"] for m in self.sent.messages])


if __name__ == "__main__":
    import unittest
    unittest.main()
