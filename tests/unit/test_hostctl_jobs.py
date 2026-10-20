"""Unit: host controller jobs, their output capture, and the /jobs routes."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.contract.auth import ROLE_CLI
from bot_bottle.contract.hostctl import HOST_CONTROL, ROLE_ORCHESTRATOR
from bot_bottle.control.hostctl.client import HostControlClient
from bot_bottle.control.hostctl.jobs import FAILED, SUCCEEDED, JobRegistry
from bot_bottle.control.hostctl.runner import HostControlError, HostControlState
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.lib import log
from bot_bottle.lib.proc import run_streaming

from . import use_bottle_root


def _finish(job, timeout: float = 10.0):  # type: ignore[no-untyped-def]
    list(job.follow(timeout=timeout))
    return job


class TestJobRegistry(unittest.TestCase):
    def test_result_and_captured_output(self) -> None:
        def work() -> dict[str, object]:
            log.info("building")
            script = "import sys; print('out', flush=True); print('err', file=sys.stderr)"
            code = run_streaming([sys.executable, "-c", script])
            return {"code": code}

        job = _finish(JobRegistry().start("demo", ROLE_CLI, work))
        self.assertEqual(SUCCEEDED, job.state)
        lines = [e["line"] for e in job.events if e["type"] == "log"]
        self.assertEqual(["bot-bottle: building", "out", "err"], lines)
        self.assertEqual(("result", {"code": 0}), (job.events[-1]["type"], job.events[-1]["value"]))
        self.assertEqual(list(range(len(job.events))), [e["seq"] for e in job.events])

    def test_errors_become_error_events(self) -> None:
        registry = JobRegistry()

        def boom() -> dict[str, object]:
            raise RuntimeError("nope")

        def dies() -> dict[str, object]:
            log.die("bring-up failed")

        failed = _finish(registry.start("a", ROLE_CLI, boom))
        died = _finish(registry.start("b", ROLE_CLI, dies))
        self.assertEqual(FAILED, failed.state)
        self.assertEqual("RuntimeError: nope", failed.events[-1]["message"])
        self.assertEqual("bring-up failed", died.events[-1]["message"])

    def test_concurrent_jobs_do_not_interleave_or_leak_into_the_caller(self) -> None:
        registry = JobRegistry()
        gate = threading.Barrier(2)

        def work(name: str):  # type: ignore[no-untyped-def]
            def run() -> dict[str, object]:
                gate.wait()
                for i in range(50):
                    log.info(f"{name}-{i}")
                return {}
            return run

        a = registry.start("a", ROLE_CLI, work("a"))
        b = registry.start("b", ROLE_CLI, work("b"))
        _finish(a)
        _finish(b)
        for job, name in ((a, "a"), (b, "b")):
            lines = [e["line"] for e in job.events if e["type"] == "log"]
            self.assertEqual([f"bot-bottle: {name}-{i}" for i in range(50)], lines)
        self.assertFalse(log.redirected())

    def test_only_the_owner_sees_a_job(self) -> None:
        registry = JobRegistry()
        job = _finish(registry.start("a", ROLE_CLI, lambda: {}))
        self.assertIs(job, registry.get(job.id, ROLE_CLI))
        self.assertIsNone(registry.get(job.id, ROLE_ORCHESTRATOR))
        self.assertEqual([], registry.list(ROLE_ORCHESTRATOR))

    def test_finished_jobs_are_dropped_after_retention(self) -> None:
        registry = JobRegistry(retention=0.0)
        job = _finish(registry.start("a", ROLE_CLI, lambda: {}))
        time.sleep(0.01)
        self.assertIsNone(registry.get(job.id, ROLE_CLI))

    def test_a_late_reader_sees_the_whole_history(self) -> None:
        release = threading.Event()

        def work() -> dict[str, object]:
            log.info("first")
            release.wait(5)
            log.info("second")
            return {}

        job = JobRegistry().start("a", ROLE_CLI, work)
        early = job.follow(timeout=5)
        self.assertEqual("bot-bottle: first", next(early)["line"])
        release.set()
        late = [e.get("line") for e in job.follow(timeout=5)]
        self.assertEqual(["bot-bottle: first", "bot-bottle: second", None], late)


class TestJobsRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(use_bottle_root(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.key = "k" * 32
        self.server = HostControlServer(("127.0.0.1", 0), "b", self.key)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.state = HostControlState(pid=0, port=self.server.server_address[1], build_id="b")
        self.cli = HostControlClient(self.state, HOST_CONTROL.mint(ROLE_CLI, self.key))

    def test_requires_a_token(self) -> None:
        with self.assertRaises(HostControlError) as cm:
            HostControlClient(self.state, "bogus").jobs()
        self.assertIn("401", str(cm.exception))

    def test_list_get_and_stream(self) -> None:
        def work() -> dict[str, object]:
            log.info("hello")
            return {"ok": True}

        job = self.server.jobs.start("demo", ROLE_CLI, work)
        lines: list[str] = []
        final = self.cli.follow(job.id, lines.append)
        self.assertEqual(["bot-bottle: hello"], lines)
        self.assertEqual({"ok": True}, final["value"] if final else None)
        self.assertEqual([job.id], [j["id"] for j in self.cli.jobs()])
        self.assertEqual("succeeded", self.cli.get(f"/jobs/{job.id}")["state"])

    def test_other_roles_cannot_see_the_job(self) -> None:
        job = _finish(self.server.jobs.start("demo", ROLE_CLI, lambda: {}))
        other = HostControlClient(self.state, HOST_CONTROL.mint(ROLE_ORCHESTRATOR, self.key))
        self.assertEqual([], other.jobs())
        with self.assertRaises(HostControlError):
            other.get(f"/jobs/{job.id}")

    def test_ctrl_c_detaches_without_a_result(self) -> None:
        with mock.patch.object(HostControlClient, "events", side_effect=KeyboardInterrupt):
            self.assertIsNone(self.cli.follow("any", lambda line: None))


if __name__ == "__main__":
    unittest.main()
