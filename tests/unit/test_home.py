"""Bare `bot-bottle`: the home menu's status strip, where its cursor starts,
what each entry runs, the Agents list's keys, and the no-terminal path."""

from __future__ import annotations

import io
import unittest
from typing import Any
from unittest import mock

from bot_bottle.cli import screen
from bot_bottle.cli.commands import home
from bot_bottle.cli.commands.home import Bot, Home, Snapshot
from bot_bottle.cli.screen import Screen
from bot_bottle.lib import log


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _ready(**kw: Any) -> Snapshot:
    snap = Snapshot(agents=["claude", "codex"], services=True, backend="firecracker",
                    console="console linked", starters={"claude": True, "codex": True,
                                                        "pi": False})
    for k, v in kw.items():
        setattr(snap, k, v)
    return snap


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(screen, "size", return_value=(120, 40)))
        self.out = _Tty()
        self.view = Screen(self.out, color=False)
        self.ran: list[tuple[str, list[str]]] = []
        self.started = 0

    def run_home(self, keys: list[str], snap: Snapshot) -> int:
        feed = iter(keys + ["q"])

        def key(_timeout: float | None = None) -> str | None:
            return next(feed)

        def run(name: str, argv: list[str]) -> int:
            self.ran.append((name, argv))
            return 0

        def start() -> int:
            self.started += 1
            return 0

        h = Home(self.view, keys=key, snapshot=lambda: snap, run=run, start=start)
        return h.run()

    def frames(self) -> str:
        return screen.strip(self.out.getvalue())


class TestStrip(_Home):
    def test_a_ready_host(self) -> None:
        self.run_home([], _ready())
        self.assertIn("✓ host ready · firecracker · console linked", self.frames())
        self.assertIn("› Start a bottled agent", self.frames())

    def test_what_is_missing_and_the_backend_command(self) -> None:
        self.run_home([], _ready(agents=[], services=False, backend_ready=False))
        text = self.frames()
        flat = " ".join(text.split())  # the strip wraps to its column
        self.assertIn("✗ no agents set up · services not running · backend firecracker not ready",
                      flat)
        self.assertIn("install --backend=firecracker", flat)

    def test_the_cursor_starts_on_the_fix(self) -> None:
        self.run_home(["enter"], _ready(agents=[]))
        self.assertEqual([("setup", [])], self.ran)
        self.ran.clear()
        self.run_home(["enter"], _ready(services=False))
        self.assertEqual([("up", [])], self.ran)


class TestAFailedStart(_Home):
    def test_its_error_is_a_note_here_and_the_menu_redraws_in_full(self) -> None:
        class FailingStart:
            def __init__(self, *_: Any, **__: Any) -> None:
                pass

            def run(self) -> int:
                log.die("launch: invalid bottle name 'x'")

        def run(_name: str, _argv: list[str]) -> int:
            return 0

        h = Home(self.view, keys=lambda _t=None: "q", snapshot=_ready, run=run,
                 start=lambda: 0)
        with mock.patch("bot_bottle.cli.commands.guided_view.StartView", FailingStart), \
             mock.patch("bot_bottle.manifest.ManifestIndex.resolve"):
            self.assertEqual(1, h._start())  # pylint: disable=protected-access
        self.assertIn("error: launch: invalid bottle name 'x'", h.notes)
        self.assertTrue(self.view.resized())


class TestEntries(_Home):
    def test_start_runs_the_guided_start(self) -> None:
        self.run_home(["enter"], _ready())
        self.assertEqual(1, self.started)

    def test_approvals_runs_supervise(self) -> None:
        self.run_home(["down", "down", "enter"], _ready(approvals=2))
        self.assertIn("2 waiting", self.frames())
        self.assertEqual([("supervise", [])], self.ran)

    def test_host_shows_status_when_the_services_run(self) -> None:
        with mock.patch.object(home, "_pause"):
            self.run_home(["down", "down", "down", "enter"], _ready())
        self.assertEqual([("status", [])], self.ran)

    def test_quit_leaves_the_screen(self) -> None:
        self.assertEqual(0, self.run_home([], _ready()))
        self.assertFalse(self.view.on)


class TestAgents(_Home):
    BOTS = [Bot("a1", "claude", "flaky", True, needs_you=True), Bot("s1", "codex", "site", False)]

    def agents_keys(self, *keys: str) -> list[str]:
        return ["down", "enter", *keys, "left"]

    def test_counts_and_needs_you(self) -> None:
        self.run_home(self.agents_keys(), _ready(bots=list(self.BOTS)))
        text = self.frames()
        self.assertIn("1 running · 1 needs you · 1 stopped", text)
        self.assertIn("running · needs you", text)

    def test_enter_attaches_a_running_one(self) -> None:
        self.run_home(self.agents_keys("enter"), _ready(bots=list(self.BOTS)))
        self.assertEqual([("attach", ["a1"])], self.ran)

    def test_r_resumes_a_stopped_one(self) -> None:
        self.run_home(self.agents_keys("down", "r"), _ready(bots=list(self.BOTS)))
        self.assertEqual([("resume", ["s1"])], self.ran)

    def test_x_stops_after_yes(self) -> None:
        with mock.patch.object(home, "stop_bot") as stop:
            self.run_home(self.agents_keys("x", "n", "x", "y"), _ready(bots=list(self.BOTS)))
        stop.assert_called_once_with("a1")

    def test_n_renames(self) -> None:
        self.run_home(self.agents_keys("n", *"new", "enter"), _ready(bots=list(self.BOTS)))
        self.assertEqual([("rename", ["a1", "new"])], self.ran)

    def test_no_bots(self) -> None:
        self.run_home(["down", "enter", "left"], _ready())
        self.assertIn("no bottled agents yet", self.frames())


class TestWindow(unittest.TestCase):
    def test_keeps_the_cursor_in_view(self) -> None:
        self.assertEqual((0, 5), home.window(20, 0, 5))
        self.assertEqual((8, 5), home.window(20, 10, 5))
        self.assertEqual((15, 5), home.window(20, 19, 5))
        self.assertEqual((0, 3), home.window(3, 2, 10))

    def test_the_architect_is_not_listed(self) -> None:
        rows = [("x1", "bottle-architect", "", "", "running", "", "", "", ""),
                ("a1", "claude", "flaky", "", "running", "", "", "", "")]
        with mock.patch("bot_bottle.cli.commands.bottled_agents._running",
                        return_value=(rows, [])), \
             mock.patch("bot_bottle.cli.commands.bottled_agents.stopped_table",
                        return_value=((), [])):
            self.assertEqual(["a1"], [b.id for b in home.list_bots()])


class TestWithoutATerminal(unittest.TestCase):
    def test_bare_prints_status_and_a_hint(self) -> None:
        with mock.patch.object(screen, "usable", return_value=False), \
             mock.patch.object(home, "_run_command", return_value=1) as run, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(1, home.cmd_home([]))
        run.assert_called_once_with("status", [])
        self.assertIn("opens the home menu", out.getvalue())


if __name__ == "__main__":
    unittest.main()
