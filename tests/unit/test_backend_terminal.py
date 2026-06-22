"""Unit tests for backend/terminal.py palette and shell-script helpers."""

from __future__ import annotations

import unittest

from bot_bottle.backend.terminal import exec_shell_script, palette_printf


class TestPalettePrintf(unittest.TestCase):
    def test_known_color_returns_printf(self):
        cmd = palette_printf("red")
        self.assertTrue(cmd.startswith("printf '"))
        self.assertIn("\\033]4;9;", cmd)   # bright-red slot
        self.assertIn("\\033]4;1;", cmd)   # normal-red slot
        self.assertIn("\\033]11;", cmd)    # default background tint

    def test_color_sets_both_palette_slots(self):
        cmd = palette_printf("blue")
        self.assertIn("\\033]4;12;", cmd)  # bright-blue slot
        self.assertIn("\\033]4;4;", cmd)   # normal-blue slot

    def test_unknown_color_returns_empty(self):
        self.assertEqual("", palette_printf(""))
        self.assertEqual("", palette_printf("neon-pink"))

    def test_all_named_colors_produce_output(self):
        colors = [
            "red", "green", "yellow", "blue", "magenta",
        ]
        for color in colors:
            with self.subTest(color=color):
                self.assertTrue(palette_printf(color))


class TestExecShellScript(unittest.TestCase):
    _ARGV = ["smolvm", "machine", "exec", "--name", "x", "--", "claude"]

    def test_no_decoration_returns_none(self):
        self.assertIsNone(exec_shell_script(self._ARGV))
        self.assertIsNone(exec_shell_script(self._ARGV, terminal_title="", terminal_color=""))

    def test_title_only_uses_exec(self):
        script = exec_shell_script(self._ARGV, terminal_title="my-agent")
        assert script is not None
        self.assertIn("printf", script)
        self.assertIn("my-agent", script)
        self.assertIn("exec ", script)
        # No palette reset when there's no color
        self.assertNotIn("\\033]104", script)

    def test_color_only_sets_palette_and_resets(self):
        script = exec_shell_script(self._ARGV, terminal_color="green")
        assert script is not None
        self.assertIn("\\033]4;", script)   # indexed palette
        self.assertIn("\\033]11;", script)  # background tint
        self.assertIn("\\033]104", script)  # palette reset
        self.assertIn("\\033]111", script)  # background reset
        # No exec-replace when palette is active (shell must survive for reset)
        parts = script.split("; ")
        agent_part = next(p for p in parts if "smolvm" in p)
        self.assertFalse(agent_part.startswith("exec "))

    def test_title_and_color_both_appear(self):
        script = exec_shell_script(self._ARGV, terminal_title="bot", terminal_color="magenta")
        assert script is not None
        self.assertIn("bot", script)
        self.assertIn("\\033]4;", script)
        self.assertIn("\\033]11;", script)
        self.assertIn("\\033]104", script)
        self.assertIn("\\033]111", script)

    def test_title_with_special_chars_is_quoted(self):
        script = exec_shell_script(self._ARGV, terminal_title="my agent's label")
        assert script is not None
        self.assertNotIn("my agent's label", script)  # must be shell-quoted


if __name__ == "__main__":
    unittest.main()
