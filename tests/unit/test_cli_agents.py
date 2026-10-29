"""`bot-bottle agents`: the agent definitions, and `agents default <agent>`."""

from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from bot_bottle.cli.commands import agents


def _run(argv: list[str], *, tty: bool = False, default: str = "") -> tuple[int, str]:
    out = io.StringIO()
    out.isatty = lambda: tty  # type: ignore[method-assign]
    index = MagicMock(all_agent_names=["claude", "pi"])
    with patch("bot_bottle.cli.commands.agents.ManifestIndex.resolve", return_value=index), \
            patch("bot_bottle.cli.commands.quick.default_agent", return_value=default), \
            patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
        code = agents.cmd_agents(argv)
    return code, out.getvalue()


class TestAgents(unittest.TestCase):
    def test_lists_them_marking_the_default_in_a_terminal(self) -> None:
        self.assertEqual((0, "claude\npi\n"), _run([], default="pi"))
        self.assertEqual("claude\npi  (default)\n", _run([], tty=True, default="pi")[1])

    def test_default_sets_it(self) -> None:
        with patch("bot_bottle.cli.commands.quick.cmd_default", return_value=0) as set_default:
            self.assertEqual(0, agents.cmd_agents(["default", "pi"]))
        set_default.assert_called_once_with(["pi"])

    def test_an_unknown_argument(self) -> None:
        self.assertEqual(2, _run(["bogus"])[0])

    def test_registered_and_the_old_names_gone(self) -> None:
        from bot_bottle.cli.commands import _HANDLERS

        self.assertEqual("agents:cmd_agents", _HANDLERS["agents"])
        for gone in ("list", "default", "quick", "backend", "login", "remote", "approve",
                     "stopped"):
            self.assertNotIn(gone, _HANDLERS)


if __name__ == "__main__":
    unittest.main()
