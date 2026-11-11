"""Unit: top-level CLI dispatch in bot_bottle.cli.main (ADR 0004).

`cli/__init__.py` is dispatch + exit-code mapping, not interactive I/O,
so it carries real unit tests rather than being omitted like the
`cli/init` / `cli/tui` shells."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import bot_bottle.cli as climod
from bot_bottle.cli import main
from bot_bottle.lib.log import Die
from bot_bottle.manifest import ManifestError


class TestMainDispatch(unittest.TestCase):
    def test_no_args_opens_home_and_a_flag_starts_an_agent_here(self) -> None:
        homes: list[list[str]] = []
        quicks: list[list[str]] = []

        def home(rest: list[str]) -> int:
            homes.append(rest)
            return 0

        def quick(rest: list[str]) -> int:
            quicks.append(rest)
            return 0

        with patch("bot_bottle.cli.__main__.HOME", home), \
                patch("bot_bottle.cli.__main__.QUICK", quick):
            self.assertEqual(0, main([]))
            self.assertEqual(0, main(["-v"]))
        self.assertEqual([[]], homes)
        self.assertEqual([["-v"]], quicks)

    def test_an_agent_name_starts_that_agent(self) -> None:
        seen: list[list[str]] = []

        def quick(rest: list[str]) -> int:
            seen.append(rest)
            return 0

        with patch("bot_bottle.cli.__main__.QUICK", quick), \
                patch("bot_bottle.cli.__main__._is_agent", return_value=True):
            main(["pi"])
        self.assertEqual([["pi"]], seen)

    def test_help_flags_return_0(self) -> None:
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, main(["-h"]))
            self.assertEqual(0, main(["--help"]))

    def test_unknown_command_dies(self) -> None:
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(Die):
                main(["definitely-not-a-command"])

    def test_handler_return_code_passthrough(self) -> None:
        def handler(_rest: list[str]) -> int:
            return 7

        with patch.dict(climod.COMMANDS, {"x": handler}):
            self.assertEqual(7, main(["x"]))

    def test_handler_none_return_becomes_0(self) -> None:
        def handler(_rest: list[str]) -> int | None:
            return None

        with patch.dict(climod.COMMANDS, {"x": handler}):
            self.assertEqual(0, main(["x"]))

    def test_args_forwarded_to_handler(self) -> None:
        seen: list[list[str]] = []

        def handler(rest: list[str]) -> int:
            seen.append(rest)
            return 0

        with patch.dict(climod.COMMANDS, {"x": handler}):
            main(["x", "a", "b"])
        self.assertEqual([["a", "b"]], seen)

    def test_missing_env_var_error_maps_to_1(self) -> None:
        from bot_bottle.errors import MissingEnvVarError

        def boom(_rest: list[str]) -> int:
            raise MissingEnvVarError("MY_VAR", "MY_VAR is not set")

        with patch.dict(climod.COMMANDS, {"x": boom}), patch("sys.stderr", io.StringIO()):
            self.assertEqual(1, main(["x"]))

    def test_manifest_error_maps_to_1(self) -> None:
        def boom(_rest: list[str]) -> int:
            raise ManifestError("bad manifest")

        with patch.dict(climod.COMMANDS, {"x": boom}), patch("sys.stderr", io.StringIO()):
            self.assertEqual(1, main(["x"]))

    def test_die_maps_to_its_code(self) -> None:
        def boom(_rest: list[str]) -> int:
            raise Die(3)

        with patch.dict(climod.COMMANDS, {"x": boom}):
            self.assertEqual(3, main(["x"]))

    def test_keyboard_interrupt_maps_to_130(self) -> None:
        def boom(_rest: list[str]) -> int:
            raise KeyboardInterrupt()

        with patch.dict(climod.COMMANDS, {"x": boom}):
            self.assertEqual(130, main(["x"]))


class TestNoMigrationGate(unittest.TestCase):
    """The host controller migrates the host DB when it starts (ADR 0020), so
    the CLI never asks and never opens it."""

    def test_a_command_runs_without_opening_the_host_db(self) -> None:
        def handler(_rest: list[str]) -> int:
            return 0

        with patch.dict(climod.COMMANDS, {"x": handler}), \
                patch("sqlite3.connect") as connect:
            self.assertEqual(0, main(["x"]))
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
