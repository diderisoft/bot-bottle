"""The bottle architect's warm bottle (PRD 0117): booted once and reused,
a fresh headless call per suggestion, torn down when idle, and any harness.

The backend is faked: `prepare` returns a plan and `launch` yields a bottle
whose `agent_argv` records what it was asked; `subprocess.run` is faked too.
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self.assertIn("task one", first)
        self.assertNotIn("--model", second)
        self.assertIn("task two", second)
        self.assertEqual(architect_bottle.TIMEOUT_SECONDS,
                         self.subprocess_run.call_args.kwargs["timeout"])

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

    def test_a_changed_harness_restarts_the_bottle(self) -> None:
        backend = FakeBackend("claude")
        warm = self._bottle(backend)
        warm.run(_architect(), "t")
        with self.assertRaisesRegex(ArchitectError, "now names codex"):
            warm.run(_architect("codex"), "t")
        self.assertTrue(backend.down.wait(5))

    def test_idle_seconds_and_model_args(self) -> None:
        with mock.patch.dict(architect_bottle.os.environ, {architect_bottle.IDLE_ENV: "30"}):
            self.assertEqual(30.0, architect_bottle.idle_seconds())
        with mock.patch.dict(architect_bottle.os.environ, {architect_bottle.IDLE_ENV: "x"}):
            self.assertEqual(architect_bottle.DEFAULT_IDLE_SECONDS, architect_bottle.idle_seconds())
        with mock.patch.dict(architect_bottle.os.environ, {architect_bottle.IDLE_ENV: ""}):
            self.assertEqual(architect_bottle.DEFAULT_IDLE_SECONDS, architect_bottle.idle_seconds())
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


if __name__ == "__main__":
    unittest.main()
