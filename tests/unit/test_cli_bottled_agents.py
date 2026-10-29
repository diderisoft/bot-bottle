"""`bot-bottle bottled-agents` (`bots`): bottled agents, running and stopped,
and the old `stopped` and `active` names (ADR 0009)."""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

from bot_bottle.bottle_state import BottleMetadata, mark_preserved, metadata_path, write_metadata
from bot_bottle.cli.commands import bottled_agents, stopped


def _bottle(identity: str, *, label: str = "", bottles: tuple[str, ...] = (),
            kept: bool = True, age: float = 0.0) -> None:
    write_metadata(BottleMetadata(identity=identity, agent_name="claude", cwd="", copy_cwd=False,
                                  started_at="2026-10-28T00:00:00Z", label=label,
                                  backend="macos-container", bottle_names=bottles))
    if kept:
        mark_preserved(identity)
    when = time.time() - age
    paths = [metadata_path(identity), metadata_path(identity).parent]
    if kept:
        paths.append(metadata_path(identity).parent / ".preserve")
    for path in paths:
        os.utime(path, (when, when))


def _running(slug: str) -> SimpleNamespace:
    return SimpleNamespace(slug=slug, agent_name="pi", label="", color="", backend_name="docker",
                           services=(), started_at="")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def run_cmd(self, argv: list[str], *, live: tuple[str, ...] = (),
                held: dict[str, dict[str, object]] | None = None, tty: bool = False,
                cmd: Callable[[list[str]], int] = bottled_agents.cmd_bottled_agents,
                ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: tty  # type: ignore[method-assign]
        with patch.object(bottled_agents, "enumerate_active_agents",
                          return_value=[_running(s) for s in live]), \
                patch.object(bottled_agents, "held_bottles", return_value=dict(held or {})), \
                patch.object(bottled_agents, "settings_from_env"), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            status = cmd(argv)
        return status, out.getvalue(), err.getvalue()


class TestBottledAgents(_Case):
    def test_running_first_then_stopped_newest_first(self) -> None:
        _bottle("old", age=3600)
        _bottle("new", label="fix tests", bottles=("claude", "gitea"))
        _bottle("crashed", kept=False, age=60)
        _bottle("up", bottles=("pi",))
        status, out, err = self.run_cmd(
            [], live=("up",), held={"up": {"session": {"running": True}}})
        self.assertEqual(0, status)
        rows = [line.split("\t") for line in out.splitlines()]
        self.assertEqual(["up", "new", "crashed", "old"], [r[0] for r in rows])
        self.assertEqual(["up", "pi", "-", "pi", "running", "open", "-", "docker"], rows[0])
        self.assertEqual(["new", "claude", "fix tests", "claude,gitea", "stopped", "-"],
                         rows[1][:6])
        self.assertEqual(["stopped", "left over", "stopped"], [r[4] for r in rows[1:]])
        self.assertEqual("macos-container", rows[1][7])
        self.assertIn("resume <bottled agent>", err)

    def test_filters(self) -> None:
        _bottle("gone")
        _bottle("up")
        _, out, _ = self.run_cmd(["--running"], live=("up",))
        self.assertEqual(["up"], [line.split("\t")[0] for line in out.splitlines()])
        _, out, err = self.run_cmd(["--stopped"], live=("up",))
        self.assertEqual(["gone"], [line.split("\t")[0] for line in out.splitlines()])
        _, out, err = self.run_cmd(["--running"])
        self.assertEqual(("", True), (out, "no running bottled agents" in err))

    def test_a_terminal_gets_an_aligned_table(self) -> None:
        _bottle("a-much-longer-identity", label="a-much-longer-identity")
        _bottle("short", label="fix tests", bottles=("claude",), kept=False, age=60)
        _, out, _ = self.run_cmd([], tty=True)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("BOTTLED AGENT"))
        self.assertNotIn("TERMINAL", lines[0])  # blank in every row
        agent_col = lines[0].index("  AGENT") + 2
        self.assertEqual({agent_col}, {line.index("claude") for line in lines[1:]})
        self.assertEqual(1, lines[1].count("a-much-longer-identity"))

    def test_a_dir_without_metadata_is_not_a_bottled_agent(self) -> None:
        from bot_bottle.paths import bot_bottle_root

        (bot_bottle_root() / "state" / "junk").mkdir(parents=True)
        _, out, err = self.run_cmd([])
        self.assertEqual("", out)
        self.assertIn("no bottled agents", err)

    def test_help_and_bad_flags(self) -> None:
        status, _, err = self.run_cmd(["--help"])
        self.assertEqual(0, status)
        self.assertIn("usage: bot-bottle bottled-agents", err)
        self.assertEqual(2, self.run_cmd(["--nope"])[0])

    def test_registered_under_three_names(self) -> None:
        from bot_bottle.cli.commands import _HANDLERS

        for name in ("bottled-agents", "bots", "ba"):
            self.assertEqual("bottled_agents:cmd_bottled_agents", _HANDLERS[name])


class TestOldNames(_Case):
    def test_stopped_is_bots_stopped_with_a_note(self) -> None:
        _bottle("gone")
        _bottle("up")
        _, out, err = self.run_cmd([], live=("up",), cmd=stopped.cmd_stopped)
        self.assertEqual(["gone"], [line.split("\t")[0] for line in out.splitlines()])
        self.assertIn("is now `bot-bottle bots --stopped`", err)


if __name__ == "__main__":
    unittest.main()
