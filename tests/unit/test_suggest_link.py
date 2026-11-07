"""The console's bottle suggestion over the link (suggest_link): a job that
answers at once, its status, and accepting it (writing a new bottle, never
one the console sends)."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import remote_actions, suggest_link
from bot_bottle.suggest.proposal import (
    Proposal, ProposalAdditions, ProposalCredential, ProposalEdit, ProposalEgress, ProposalEnv,
    ProposalNewBottle, ProposalRepo,
)
from bot_bottle.suggest.write import SuggestWriteError, write_bottle


def _proposal(option: str = "reuse", bottles: list[str] | None = None,
              new: ProposalNewBottle | None = None,
              additions: ProposalAdditions | None = None) -> Proposal:
    return Proposal(option=option, bottles=bottles if bottles is not None else ["basic"],
                    new_bottle=new, additions=additions or ProposalAdditions(),
                    similar=[], notes=["a note"])


def _wait(job_id: str) -> dict[str, Any]:
    for _ in range(200):
        st = suggest_link.status(job_id)
        if st["state"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError("the job never finished")


class TestSuggestJobs(unittest.TestCase):
    def setUp(self) -> None:
        suggest_link._jobs.clear()  # pylint: disable=protected-access

    def test_a_job_runs_and_reports_its_summary(self) -> None:
        add = ProposalAdditions(egress=[ProposalEgress(host="pypi.org")],
                                repos=[ProposalRepo(grant="gitea.x/o/r:write")],
                                env=[ProposalEnv(name="DEBUG")],
                                credentials=[ProposalCredential(name="PYPI_TOKEN",
                                                                route="pypi.org")])
        p = _proposal("stack+additions", ["basic"], ProposalNewBottle(name="todo-api"), add)
        job = suggest_link.start("build a todo api", suggest=lambda t, c, _turns: p)
        st = _wait(job)
        self.assertEqual("done", st["state"])
        s = st["proposal"]
        self.assertEqual(["pypi.org"], s["egress"])
        self.assertEqual(["gitea.x/o/r:write"], s["repos"])
        self.assertEqual(["DEBUG"], s["env"])
        self.assertEqual([{"name": "PYPI_TOKEN", "route": "pypi.org"}], s["credentials"])
        self.assertTrue(s["stacked"])
        self.assertTrue(s["writes"])

    def test_a_failed_suggestion_says_so_without_details(self) -> None:
        def boom(task: str, cwd: Path | None, turns: list[Any]) -> Proposal:
            raise RuntimeError("secret detail")
        st = _wait(suggest_link.start("t", suggest=boom))
        self.assertEqual("error", st["state"])
        self.assertNotIn("secret detail", st["error"])

    def test_bad_tasks_and_too_many_jobs_are_refused(self) -> None:
        for bad in ("", "   ", "x" * (suggest_link.MAX_TASK_CHARS + 1)):
            with self.subTest(task=bad[:10]), self.assertRaises(ValueError):
                suggest_link.start(bad, suggest=lambda t, c, _turns: _proposal())
        hold = __import__("threading").Event()

        def slow(task: str, cwd: Path | None, turns: list[Any]) -> Proposal:
            del task, cwd, turns
            hold.wait(5)
            return _proposal()
        for _ in range(suggest_link.MAX_JOBS):
            suggest_link.start("t", suggest=slow)
        with self.assertRaises(ValueError):
            suggest_link.start("t", suggest=slow)
        hold.set()

    def test_a_stuck_job_times_out_and_unknown_jobs_error(self) -> None:
        hold = __import__("threading").Event()
        job = suggest_link.start("t", suggest=lambda t, c, _turns: (hold.wait(5), _proposal())[1],
                                 clock=lambda: 0.0)
        st = suggest_link.status(job, clock=lambda: suggest_link.RUN_SECONDS + 1)
        self.assertEqual("error", st["state"])
        hold.set()
        self.assertEqual("error", suggest_link.status("nope")["state"])


class TestAccept(unittest.TestCase):
    def setUp(self) -> None:
        suggest_link._jobs.clear()  # pylint: disable=protected-access

    def _done(self, p: Proposal) -> str:
        job = suggest_link.start("t", suggest=lambda t, c, _turns: p)
        _wait(job)
        return job

    def test_reuse_launches_the_existing_bottles_and_writes_nothing(self) -> None:
        write = mock.Mock()
        job = self._done(_proposal("stack", ["basic", "python"]))
        self.assertEqual({"bottles": ["basic", "python"]}, suggest_link.accept(job, write=write))
        write.assert_not_called()

    def test_a_new_bottle_is_written_and_launched(self) -> None:
        write = mock.Mock()
        job = self._done(_proposal("new-extending", [], ProposalNewBottle(name="todo-api",
                                                                          extends="basic")))
        self.assertEqual({"bottles": ["todo-api"], "written": "todo-api"},
                         suggest_link.accept(job, write=write))
        write.assert_called_once()
        # Accepting again answers the same, without writing twice.
        self.assertEqual({"bottles": ["todo-api"]}, suggest_link.accept(job, write=write))
        write.assert_called_once()

    def test_additions_are_stacked_on_the_bottles_under_a_given_name(self) -> None:
        write = mock.Mock()
        add = ProposalAdditions(egress=[ProposalEgress(host="pypi.org")])
        job = self._done(_proposal("stack+additions", ["basic"], None, add))
        self.assertEqual({"error": "the bottle needs a name"},
                         suggest_link.accept(job, write=write))
        self.assertEqual({"bottles": ["basic", "todo-extra"], "written": "todo-extra"},
                         suggest_link.accept(job, "todo-extra", write=write))

    def test_a_write_refusal_is_reported(self) -> None:
        def refuse(name: str, p: Proposal) -> Path:
            raise SuggestWriteError("the bottle 'x' already exists; pick another name")
        job = self._done(_proposal("new", [], ProposalNewBottle(name="x")))
        self.assertIn("already exists", suggest_link.accept(job, write=refuse)["error"])

    def test_nothing_to_accept(self) -> None:
        self.assertIn("error", suggest_link.accept("nope"))


class TestWriteBottle(unittest.TestCase):
    def test_writes_a_new_bottle_and_refuses_existing_or_bad_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("bot_bottle.manifest.versions.record_or_warn") as recorded:
            root = Path(tmp)
            p = _proposal("new", [], ProposalNewBottle(name="todo-api", about="A todo API"),
                          ProposalAdditions(egress=[ProposalEgress(host="pypi.org")]))
            path = write_bottle("todo-api", p, root=root)
            self.assertTrue(path.read_text().startswith("---\n"))
            self.assertIn("A todo API", path.read_text())
            recorded.assert_called_once()
            with self.assertRaises(SuggestWriteError):
                write_bottle("todo-api", p, root=root)
            with self.assertRaises(SuggestWriteError):
                write_bottle("Bad Name", p, root=root)


class TestLinkRequests(unittest.TestCase):
    def setUp(self) -> None:
        suggest_link._jobs.clear()  # pylint: disable=protected-access

    def test_registered_and_answering(self) -> None:
        for kind in ("bottle_suggest", "bottle_suggest_status", "bottle_suggestion_accept"):
            self.assertIn(kind, remote_actions.HOST_REQUESTS)
        with mock.patch.object(suggest_link, "_suggest", return_value=_proposal()):
            started = asyncio.run(suggest_link.bottle_suggest(
                {"request_id": "r1", "task": "build a todo api"}))
            self.assertEqual(("bottle_suggestion", "r1", "running"),
                             (started["type"], started["request_id"], started["state"]))
            job = str(started["job"])
            _wait(job)
            st = asyncio.run(suggest_link.bottle_suggest_status({"request_id": "r2", "job": job}))
            self.assertEqual(("bottle_suggestion", "done"), (st["type"], st["state"]))
            acc = asyncio.run(suggest_link.bottle_suggestion_accept(
                {"request_id": "r3", "job": job}))
            self.assertEqual(("bottle_suggestion_accepted", ["basic"]),
                             (acc["type"], acc["bottles"]))
        refused = asyncio.run(suggest_link.bottle_suggest({"request_id": "r4", "task": ""}))
        self.assertIn("error", refused)

    def _started_with(self, msg: dict[str, Any]) -> tuple[dict[str, Any], list[object]]:
        seen: list[object] = []

        def fake(task: str, cwd: Path | None, turns: list[Any]) -> Proposal:
            del task, turns
            seen.append(cwd)
            return _proposal()

        with mock.patch.object(suggest_link, "_suggest", side_effect=fake):
            reply = asyncio.run(suggest_link.bottle_suggest({"request_id": "r", **msg}))
            if "job" in reply:
                _wait(str(reply["job"]))
        return reply, seen

    def test_the_project_directory_is_passed_through(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            project = Path(home) / "project"
            project.mkdir()
            with mock.patch.object(suggest_link.Path, "home", return_value=Path(home)):
                reply, seen = self._started_with({"task": "t", "cwd": str(project)})
        self.assertEqual("running", reply["state"])
        self.assertEqual([project.resolve()], seen)

    def test_without_one_the_suggester_gets_none_not_home(self) -> None:
        _, seen = self._started_with({"task": "t"})
        self.assertEqual([None], seen)

    def test_a_bad_project_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            outside = tempfile.mkdtemp()
            self.addCleanup(Path(outside).rmdir)
            with mock.patch.object(suggest_link.Path, "home", return_value=Path(home)):
                for cwd, why in (("relative/dir", "absolute"), (home, "under your home"),
                                 (outside, "under your home"),
                                 (str(Path(home) / "gone"), "isn't a directory"), (7, "absolute")):
                    with self.subTest(cwd=cwd):
                        reply, seen = self._started_with({"task": "t", "cwd": cwd})
                        self.assertIn(why, reply.get("error", ""))
                        self.assertEqual([], seen)

    def test_the_summary_says_which_path_answered(self) -> None:
        p = _proposal()
        p.source = "rules"
        self.assertEqual("rules", suggest_link.summary(p)["source"])
        p.blocked = "it needs X"
        self.assertEqual("it needs X", suggest_link.summary(p)["blocked"])


if __name__ == "__main__":
    unittest.main()


class TestReviewOnThePage(unittest.TestCase):
    """The guided start's review over the link (PRD 0118): the gist and what
    it adds, edits decided one at a time, and replies."""

    def setUp(self) -> None:
        suggest_link._jobs.clear()  # pylint: disable=protected-access
        # pylint: disable-next=consider-using-with
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / ".bot-bottle"
        (root / "bottles").mkdir(parents=True)
        self.bottle = root / "bottles" / "team.md"
        self.bottle.write_text("---\nextends: basic\n# the team's\n---\n\nTeam.\n")
        self.enterContext(mock.patch.dict("os.environ", {"BOT_BOTTLE_ROOT": str(root)}))
        self.enterContext(mock.patch("bot_bottle.manifest.versions.record_or_warn"))
        self.calls: list[list[str]] = []

    def _job(self, *answers: Proposal) -> str:
        queue = list(answers)

        def fake(task: str, cwd: Path | None, turns: list[Any]) -> Proposal:
            del task, cwd
            self.calls.append([t.reply for t in turns])
            return queue.pop(0)
        job = suggest_link.start("t", suggest=fake)
        _wait(job)
        return job

    @staticmethod
    def _editing() -> Proposal:
        p = _proposal()
        p.edits = [ProposalEdit("team", ProposalAdditions(
            egress=[ProposalEgress("api.linear.app", ["POST"])]), why="tickets")]
        return p

    def test_the_summary_carries_the_review(self) -> None:
        s = _wait(self._job(self._editing()))["proposal"]
        self.assertTrue(s["gist"])
        edit = s["edits"][0]
        self.assertEqual(("team", "+ api.linear.app", "tickets", []),
                         (edit["bottle"], edit["adds"], edit["why"], edit["waits_on"]))
        self.assertIn("+    - host: api.linear.app", edit["diff"])
        self.assertEqual({}, s["connect"])

    def test_accept_writes_the_edit_and_then_the_bottle_can_be_used(self) -> None:
        job = self._job(self._editing())
        self.assertEqual({"error": "accept or skip each edit first"}, suggest_link.accept(job))
        st = suggest_link.decide_edit(job, "accept")
        self.assertEqual([], st["proposal"]["edits"])
        self.assertEqual(["team: edited (a new version)"], st["decided"])
        text = self.bottle.read_text()
        self.assertIn("api.linear.app", text)
        self.assertIn("# the team's", text)
        self.assertEqual({"bottles": ["basic"]}, suggest_link.accept(job))
        self.assertIn("error", suggest_link.decide_edit(job, "accept"))
        self.assertIn("error", suggest_link.decide_edit(job, "maybe"))

    def test_a_reply_asks_again_with_what_was_said_and_done(self) -> None:
        job = self._job(self._editing(), _proposal("stack", ["basic", "team"]))
        suggest_link.decide_edit(job, "skip")
        self.assertNotEqual("error", suggest_link.ask_again(job, "use team")["state"])
        st = _wait(job)
        self.assertEqual([[], ["use team"]], self.calls)
        self.assertEqual(["use team"], st["replies"])
        turn = suggest_link._jobs[job].turns[0]  # pylint: disable=protected-access
        self.assertEqual(["skipped the edit of team"], turn.events)
        self.assertEqual("error", suggest_link.ask_again(job, "")["state"])
        self.assertEqual("error", suggest_link.ask_again("nope", "hi")["state"])
        self.assertNotIn("api.linear.app", self.bottle.read_text())

    def test_the_link_requests(self) -> None:
        job = self._job(self._editing())
        out = asyncio.run(suggest_link.REQUESTS["bottle_suggestion_edit"](
            {"request_id": "r", "job": job, "decision": "skip"}))
        self.assertEqual(("bottle_suggestion", "r", []),
                         (out["type"], out["request_id"], out["proposal"]["edits"]))
        self.assertIn("bottle_suggestion_reply", remote_actions.HOST_REQUESTS)
