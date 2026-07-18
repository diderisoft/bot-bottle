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
from bot_bottle.db_store import DbStore
from bot_bottle.log import Die
from bot_bottle.manifest import ManifestError
from bot_bottle.store_manager import StoreManager


class TestMainDispatch(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(DbStore, "is_migrated", return_value=True)
        self._mock_check = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(StoreManager.reset)

    def test_no_args_prints_usage_returns_2(self) -> None:
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(2, main([]))

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


class TestMigrationGate(unittest.TestCase):
    """The dispatcher's schema-migration gate (cli/__init__.py)."""

    def setUp(self) -> None:
        # Force the "schema out of date" branch for every test here.
        patcher = patch.object(StoreManager, "is_migrated", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(StoreManager.reset)

    def test_store_command_blocks_when_stdin_cannot_confirm(self) -> None:
        # Non-TTY stdin at EOF (as in CI): the [y/N] prompt reads "" and the
        # command is refused rather than migrating silently.
        ran: list[bool] = []

        def handler(_rest: list[str]) -> int:
            ran.append(True)
            return 0

        with patch.dict(climod.COMMANDS, {"list": handler}), \
                patch("sys.stdin", io.StringIO("")), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(1, main(["list"]))
        self.assertEqual([], ran, "gated command must not dispatch")

    def test_backend_command_skips_gate(self) -> None:
        # `backend` provisions/probes the host and never opens the store, so
        # it must run even on an unmigrated DB with unanswerable stdin.
        ran: list[bool] = []

        def handler(_rest: list[str]) -> int:
            ran.append(True)
            return 0

        with patch.dict(climod.COMMANDS, {"backend": handler}), \
                patch("sys.stdin", io.StringIO("")), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, main(["backend", "status"]))
        self.assertEqual([True], ran, "exempt command must dispatch")

    def test_store_command_migrates_on_confirmation(self) -> None:
        migrated: list[bool] = []

        def handler(_rest: list[str]) -> int:
            return 0

        with patch.dict(climod.COMMANDS, {"list": handler}), \
                patch.object(StoreManager, "migrate",
                             side_effect=lambda: migrated.append(True)), \
                patch("sys.stdin", io.StringIO("y\n")), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, main(["list"]))
        self.assertEqual([True], migrated, "confirmed gate must migrate")


if __name__ == "__main__":
    unittest.main()
