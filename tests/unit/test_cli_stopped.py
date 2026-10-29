"""`bot-bottle stopped`: bottles that aren't running and can be resumed."""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot_bottle.bottle_state import BottleMetadata, mark_preserved, metadata_path, write_metadata
from bot_bottle.cli.commands import stopped


def _bottle(identity: str, *, label: str = "", bottles: tuple[str, ...] = (),
            kept: bool = True, age: float = 0.0) -> None:
    write_metadata(BottleMetadata(identity=identity, agent_name="claude", cwd="", copy_cwd=False,
                                  started_at="2026-10-28T00:00:00Z", label=label,
                                  bottle_names=bottles))
    if kept:
        mark_preserved(identity)
    when = time.time() - age
    for path in (metadata_path(identity), metadata_path(identity).parent):
        os.utime(path, (when, when))
    if kept:
        os.utime(metadata_path(identity).parent / ".preserve", (when, when))


class TestStopped(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def run_stopped(self, live: tuple[str, ...] = (), held: tuple[str, ...] = (),
                    argv: list[str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        agents = [SimpleNamespace(slug=s) for s in live]
        with patch.object(stopped, "enumerate_active_agents", return_value=agents), \
                patch.object(stopped, "held_bottles", return_value={h: {} for h in held}), \
                patch.object(stopped, "settings_from_env"), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            status = stopped.cmd_stopped(argv or [])
        return status, out.getvalue(), err.getvalue()

    def test_newest_first_without_running_ones(self) -> None:
        _bottle("old", age=3600)
        _bottle("new", label="fix tests", bottles=("claude", "gitea"))
        _bottle("crashed", kept=False, age=60)
        _bottle("running")
        _bottle("held")
        status, out, err = self.run_stopped(live=("running",), held=("held",))
        self.assertEqual(0, status)
        rows = [line.split("\t") for line in out.splitlines()]
        self.assertEqual(["new", "crashed", "old"], [r[0] for r in rows])
        self.assertEqual(["new", "claude", "fix tests", "claude,gitea"], rows[0][:4])
        self.assertEqual(["-", "-"], [rows[1][2], rows[1][3]])  # empty fields
        self.assertEqual(["kept", "left over", "kept"], [r[5] for r in rows])
        self.assertIn("resume <identity>", err)

    def test_a_terminal_gets_an_aligned_table(self) -> None:
        _bottle("a-much-longer-identity", label="a-much-longer-identity")
        _bottle("short", label="fix tests", bottles=("claude",), kept=False, age=60)
        out = io.StringIO()
        out.isatty = lambda: True  # type: ignore[method-assign]
        with patch.object(stopped, "enumerate_active_agents", return_value=[]), \
                patch.object(stopped, "held_bottles", return_value={}), \
                patch.object(stopped, "settings_from_env"), \
                patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            stopped.cmd_stopped([])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("IDENTITY"))
        # The agent column starts at the same offset on every line.
        self.assertEqual({lines[0].index("AGENT")}, {line.index("claude") for line in lines[1:]})
        # A label that only repeats the identity is left blank.
        self.assertEqual(1, lines[1].count("a-much-longer-identity"))

    def test_a_dir_without_metadata_is_not_a_bottle(self) -> None:
        from bot_bottle.paths import bot_bottle_root

        (bot_bottle_root() / "state" / "junk").mkdir(parents=True)
        _, out, err = self.run_stopped()
        self.assertEqual("", out)
        self.assertIn("no stopped bot-bottle bottles", err)

    def test_help(self) -> None:
        status, _, err = self.run_stopped(argv=["--help"])
        self.assertEqual(0, status)
        self.assertIn("usage: bot-bottle stopped", err)

    def test_registered(self) -> None:
        from bot_bottle.cli.commands import _HANDLERS

        self.assertEqual("stopped:cmd_stopped", _HANDLERS["stopped"])


if __name__ == "__main__":
    unittest.main()
