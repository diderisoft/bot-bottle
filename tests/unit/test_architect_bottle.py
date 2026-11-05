"""The bottle architect's warm bottle (PRD 0117): booted once and reused,
a fresh headless call per suggestion, torn down when idle, and any harness.

The backend is faked: `prepare` returns a plan and `launch` yields a bottle
whose `agent_argv` records what it was asked; `subprocess.run` is faked too.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import architect_bottle
from bot_bottle.control.hostctl.architect_bottle import ArchitectBottle
from bot_bottle.suggest.architect import Architect, ArchitectError


class FakeBackend:
    name = "fake"

    def __init__(self, harness: str = "claude", fail: BaseException | None = None) -> None:
        self.harness = harness
        self.fail = fail
        self.launches = 0
        self.down = threading.Event()
        self.specs: list[object] = []
        self.argv: list[list[str]] = []
        self.execs: list[tuple[str, dict[str, Any]]] = []
        self.exec_result: object = SimpleNamespace(returncode=0, stdout="", stderr="")

    def prepare(self, spec: object, *, stage_dir: Path, secrets: object) -> object:
        del stage_dir, secrets
        if self.fail is not None:
            raise self.fail
        self.specs.append(spec)
        return SimpleNamespace(slug=f"arch-{len(self.specs)}", manifest=SimpleNamespace(),
                               agent_provision=SimpleNamespace(harness=self.harness,
                                                               startup_args=("--fast",)))

    @contextlib.contextmanager
    def launch(self, plan: object):  # type: ignore[no-untyped-def]
        del plan
        self.launches += 1
        backend = self

        class Bottle:
            registry_id = "reg"

            @staticmethod
            def agent_argv(argv: list[str], *, tty: bool = True) -> list[str]:
                assert not tty
                backend.argv.append(argv)
                return ["ssh", "vm", "--", *argv]

            @staticmethod
            def agent_env() -> dict[str, str]:
                return {"HTTPS_PROXY": "http://gateway:3128"}

            @staticmethod
            def exec(script: str, **kw: Any) -> object:
                backend.execs.append((script, kw))
                if isinstance(backend.exec_result, BaseException):
                    raise backend.exec_result
                return backend.exec_result

        try:
            yield Bottle()
        finally:
            self.down.set()


def _architect(harness: str = "claude", model: str = "") -> Architect:
    return Architect(path=Path("/h/agents/bottle-architect.md"), prompt="p",
                     harness=harness, model=model)


class TestArchitectBottle(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        answer = mock.Mock(returncode=0, stdout='{"option": "reuse"}\n')
        patch = mock.patch.object(architect_bottle.subprocess, "run", return_value=answer)
        self.subprocess_run = patch.start()
        self.addCleanup(patch.stop)
        settle = mock.patch("bot_bottle.bottle_state.settle_state")
        settle.start()
        self.addCleanup(settle.stop)

    def _bottle(self, backend: FakeBackend, idle: float = 60.0) -> ArchitectBottle:
        warm = ArchitectBottle(host_root=self.root, backend_for=lambda _s: backend,  # type: ignore[arg-type,return-value]
                               secrets_file=self.root / "secrets", idle=idle)
        self.addCleanup(warm.stop)
        return warm

    def test_boots_once_and_runs_a_fresh_headless_call_per_suggestion(self) -> None:
        backend = FakeBackend()
        warm = self._bottle(backend)
        self.assertEqual('{"option": "reuse"}', warm.run(_architect(model="m1"), "task one"))
        warm.run(_architect(), "task two")
        self.assertEqual(1, backend.launches)
        self.assertEqual(2, len(backend.argv))
        first, second = backend.argv[0], backend.argv[1]
        self.assertIn("--fast", first)
        at = first.index("--model")
        self.assertEqual(["--model", "m1"], first[at:at + 2])
        self.assertTrue(first[-1].startswith("task one\n\n<!-- bot-bottle-architect-call-"))
        self.assertNotIn("--model", second)
        self.assertTrue(second[-1].startswith("task two\n"))
        self.assertEqual(architect_bottle.TIMEOUT_SECONDS,
                         self.subprocess_run.call_args.kwargs["timeout"])

    def test_the_call_carries_the_agent_env_behind_its_bare_names(self) -> None:
        # The proxy rides in the env, not argv; without it the architect has
        # no route to its model API and times out.
        self._bottle(FakeBackend()).run(_architect(), "t")
        env = self.subprocess_run.call_args.kwargs["env"]
        self.assertEqual("http://gateway:3128", env["HTTPS_PROXY"])
        self.assertEqual(os.environ.get("PATH"), env.get("PATH"))

    def test_it_runs_in_its_own_bottle_with_an_empty_workspace(self) -> None:
        backend = FakeBackend()
        self._bottle(backend).run(_architect(), "t")
        spec = backend.specs[0]
        self.assertEqual(("bottle-architect", ("bottle-architect",), False, True),
                         (spec.agent_name, spec.bottle_names, spec.copy_cwd,  # type: ignore[attr-defined]
                          spec.headless))  # type: ignore[attr-defined]
        self.assertEqual((architect_bottle.ARCHITECT_BOTTLES_DIR,),
                         spec.manifest.extra_bottle_dirs)  # type: ignore[attr-defined]
        self.assertTrue((architect_bottle.ARCHITECT_BOTTLES_DIR / "bottle-architect.md").is_file())

    def test_any_harness_runs_there(self) -> None:
        for harness in ("codex", "pi"):
            with self.subTest(harness=harness):
                backend = FakeBackend(harness)
                self._bottle(backend).run(_architect(harness), "t")
                self.assertEqual(1, backend.launches)

    def test_idle_tears_it_down_and_the_next_suggestion_boots_again(self) -> None:
        backend = FakeBackend()
        warm = self._bottle(backend, idle=0.0)
        warm.run(_architect(), "t")
        self.assertTrue(backend.down.wait(5))
        for _ in range(100):
            if warm.status()["state"] == "down":
                break
            threading.Event().wait(0.02)
        backend.down.clear()
        warm.run(_architect(), "t")
        self.assertEqual(2, backend.launches)

    def test_warm_boots_in_the_background(self) -> None:
        backend = FakeBackend()
        warm = self._bottle(backend)
        warm.warm()
        for _ in range(100):
            if warm.status()["state"] == "ready":
                break
            threading.Event().wait(0.02)
        self.assertEqual("ready", warm.status()["state"])
        warm.warm()  # already up: no second boot
        self.assertEqual(1, backend.launches)

    def test_failures_are_architect_errors(self) -> None:
        with self.assertRaisesRegex(ArchitectError, "didn't start: no images"):
            self._bottle(FakeBackend(fail=RuntimeError("no images"))).run(_architect(), "t")
        warm = self._bottle(FakeBackend())
        for result, why in ((mock.Mock(returncode=1, stdout=""), "logged in"),
                            (subprocess.TimeoutExpired("claude", 1), "didn't answer within"),
                            (OSError("no ssh"), "didn't run")):
            with self.subTest(why=why):
                failing = isinstance(result, BaseException)
                self.subprocess_run.side_effect = result if failing else None
                self.subprocess_run.return_value = result
                with self.assertRaisesRegex(ArchitectError, why):
                    warm.run(_architect(), "t")

    def test_a_timed_out_call_is_stopped_in_the_bottle_too(self) -> None:
        backend = FakeBackend()
        warm = self._bottle(backend)
        self.subprocess_run.side_effect = subprocess.TimeoutExpired("claude", 1)
        with self.assertRaisesRegex(ArchitectError, "didn't answer within"):
            warm.run(_architect(), "t")
        mark = re.search(r"bot-bottle-architect-call-[0-9a-f]{32}", " ".join(backend.argv[0]))
        assert mark is not None  # the call carries its mark
        script, kw = backend.execs[0]
        self.assertIn(f"m={mark.group(0)}\n", script)
        self.assertEqual("root", kw["user"])
        self.assertEqual(architect_bottle.STOP_SECONDS, kw["timeout"])

    def test_each_call_has_its_own_mark_and_an_answer_stops_nothing(self) -> None:
        backend = FakeBackend()
        warm = self._bottle(backend)
        warm.run(_architect(), "t")
        warm.run(_architect(), "t")
        marks = {m for argv in backend.argv
                 for m in re.findall(r"bot-bottle-architect-call-[0-9a-f]{32}", " ".join(argv))}
        self.assertEqual(2, len(marks))
        self.assertEqual([], backend.execs)

    def test_a_failed_stop_still_reports_the_timeout(self) -> None:
        for result in (RuntimeError("no exec"), SimpleNamespace(returncode=1)):
            with self.subTest(result=result):
                backend = FakeBackend()
                backend.exec_result = result
                warm = self._bottle(backend)
                self.subprocess_run.side_effect = subprocess.TimeoutExpired("claude", 1)
                with mock.patch.object(architect_bottle.log, "warn") as warn, \
                     self.assertRaisesRegex(ArchitectError, "didn't answer within"):
                    warm.run(_architect(), "t")
                self.assertIn("timed-out call", warn.call_args.args[0])

    def test_a_bottle_removed_under_it_is_replaced_and_the_call_retried(self) -> None:
        backend = FakeBackend()
        backend.exec_result = SimpleNamespace(returncode=1)  # `true` fails: it's gone
        warm = self._bottle(backend)
        self.subprocess_run.side_effect = [mock.Mock(returncode=1, stdout=""),
                                           mock.Mock(returncode=0, stdout='{"option": "x"}')]
        self.assertEqual('{"option": "x"}', warm.run(_architect(), "t"))
        self.assertEqual(2, backend.launches)

    def test_a_bottle_gone_twice_is_an_architect_error(self) -> None:
        backend = FakeBackend()
        backend.exec_result = SimpleNamespace(returncode=1)
        warm = self._bottle(backend)
        self.subprocess_run.return_value = mock.Mock(returncode=1, stdout="")
        with self.assertRaisesRegex(ArchitectError, "its bottle had stopped"):
            warm.run(_architect(), "t")
        self.assertEqual(2, backend.launches)

    def test_a_changed_harness_restarts_the_bottle(self) -> None:
        backend = FakeBackend("claude")
        warm = self._bottle(backend)
        warm.run(_architect(), "t")
        with self.assertRaisesRegex(ArchitectError, "now names codex"):
            warm.run(_architect("codex"), "t")
        self.assertTrue(backend.down.wait(5))

    def test_the_idle_time_comes_from_the_settings(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.settings import settings_from_env
        settings = settings_from_env({"BOT_BOTTLE_ARCHITECT_IDLE": "30"})
        self.assertEqual(30, settings.operation.architect_idle_seconds)
        self.assertNotIn("architect_idle_seconds",
                         architect_bottle.Settings().to_json()["operation"])  # type: ignore[operator]
        warm = ArchitectBottle(host_root=self.root,
                               backend_for=lambda _s: FakeBackend())  # type: ignore[arg-type,return-value]
        self.addCleanup(warm.stop)
        warm.warm(settings)
        self.assertEqual(30.0, warm.status()["idle_seconds"])
        self.assertEqual([], architect_bottle.model_args(""))

    def test_the_installed_bottle_is_the_suggesters_runner(self) -> None:
        architect_bottle.install(None)
        self.assertIsNone(architect_bottle.runner())
        backend = FakeBackend()
        warm = self._bottle(backend)
        architect_bottle.install(warm)
        self.addCleanup(architect_bottle.install, None)
        self.assertIs(warm, architect_bottle.current())
        run = architect_bottle.runner()
        assert run is not None
        self.assertEqual('{"option": "reuse"}', run(_architect(), "t"))



@unittest.skipUnless(os.path.isdir("/proc/self") and shutil.which("sh"), "needs Linux /proc")
class TestStopScript(unittest.TestCase):
    """The stop script against real processes: the marked one and its child
    stop; an unmarked one keeps running."""

    def _spawn(self, tag: str) -> subprocess.Popen[bytes]:
        # A shell (carrying `tag`) with a child, like the agent and its tools.
        proc = subprocess.Popen(["sh", "-c", "sleep 60 & wait", tag])  # pylint: disable=consider-using-with
        self.addCleanup(proc.kill)
        return proc

    @staticmethod
    def _children(pid: int) -> list[int]:
        found = []
        for entry in Path("/proc").iterdir():
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if entry.name.isdigit() and stat.rsplit(") ", 1)[-1].split()[1] == str(pid):
                found.append(int(entry.name))
        return found

    def test_it_stops_the_marked_tree_and_nothing_else(self) -> None:
        mark = "bot-bottle-architect-call-" + "a" * 32
        marked, other = self._spawn(mark), self._spawn("unrelated")
        for _ in range(100):
            if self._children(marked.pid) and self._children(other.pid):
                break
            threading.Event().wait(0.02)
        child = self._children(marked.pid)[0]
        script = architect_bottle._STOP_MARKED.format(mark=mark)  # pylint: disable=protected-access
        subprocess.run(["sh", "-s"], input=script.encode(), check=True, timeout=30)
        self.assertIsNotNone(marked.wait(5))
        self.assertFalse(Path(f"/proc/{child}").exists() and
                         "sleep" in Path(f"/proc/{child}/cmdline").read_text(encoding="utf-8", errors="replace"))
        self.assertIsNone(other.poll())

if __name__ == "__main__":
    unittest.main()
