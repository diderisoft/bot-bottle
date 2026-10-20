"""Unit: _launch_bottle StaleImageError handling.

Exercises prelaunch_checks / backend.launch flow:
  - headless mode → die on stale
  - interactive mode, user declines → stop without launching
  - interactive mode, user confirms → skip stale check and launch once
"""

from __future__ import annotations

import io
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.image_cache import StaleImageError
from bot_bottle.lib.log import Die


def _fake_plan() -> Any:
    provision = SimpleNamespace(startup_args=())
    return cast(Any, SimpleNamespace(
        agent_provision=provision,
        agent_provider_template="claude",
        slug="dev-abc",
    ))


def _ok_cm(bottle: Any) -> MagicMock:
    """Return a context-manager mock that yields `bottle`."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=bottle)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestLaunchBottleStaleHandling(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="cli-stale-test.")

    def _spec(self) -> Any:
        from bot_bottle.backend import BottleSpec
        from bot_bottle.manifest import ManifestIndex
        idx = ManifestIndex.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        return BottleSpec(
            manifest=idx,
            agent_name="demo",
            copy_cwd=False,
            user_cwd=self._tmp,
            identity="dev-abc",
        )

    def _run_launch(self, **patch_kwargs: Any) -> int:
        import bot_bottle.cli.commands.start as start_mod
        spec = self._spec()
        with patch.object(start_mod, "prepare_with_preflight",
                          return_value=(_fake_plan(), "dev-abc")), \
             patch.object(start_mod, "settle_state"), \
             patch.object(start_mod, "info"):
            return start_mod._launch_bottle(
                spec,
                Settings(),
                dry_run=False,
                backend_name="docker",
                **patch_kwargs,
            )

    def test_headless_stale_calls_die(self) -> None:
        """In headless mode (assume_yes=True), a StaleImageError from prelaunch_checks must call die()."""
        import bot_bottle.cli.commands.start as start_mod

        backend_mock = MagicMock()
        backend_mock.prelaunch_checks.side_effect = StaleImageError("image is 5 day(s) old")

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "die", side_effect=Die()):
            with self.assertRaises(Die):
                self._run_launch(assume_yes=True)

        backend_mock.launch.assert_not_called()

    def test_interactive_user_declines_stops_before_launch(self) -> None:
        """Interactive user answering 'n' → launch is never called."""
        import bot_bottle.cli.commands.start as start_mod

        backend_mock = MagicMock()
        backend_mock.prelaunch_checks.side_effect = StaleImageError("image is 5 day(s) old")

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "read_tty_line", return_value="n"), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = self._run_launch(assume_yes=False)

        self.assertEqual(0, rc)
        backend_mock.launch.assert_not_called()

    def test_interactive_user_confirms_launches_once(self) -> None:
        """Interactive user answering 'y' → prelaunch stale error is bypassed; launch called once."""
        import bot_bottle.cli.commands.start as start_mod

        bottle_mock = MagicMock()
        bottle_mock.name = "dev-abc"

        backend_mock = MagicMock()
        backend_mock.prelaunch_checks.side_effect = StaleImageError("image is 5 day(s) old")
        backend_mock.launch.return_value = _ok_cm(bottle_mock)

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "read_tty_line", return_value="y"), \
             patch.object(start_mod, "attach_agent", return_value=0), \
             patch.object(start_mod, "capture_claude_session_state"), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = self._run_launch(assume_yes=False)

        self.assertEqual(0, rc)
        backend_mock.prelaunch_checks.assert_called_once()
        backend_mock.launch.assert_called_once()

    def test_interactive_yes_uppercase_also_accepted(self) -> None:
        """'Y' or 'YES' should also be accepted as confirmation."""
        import bot_bottle.cli.commands.start as start_mod

        bottle_mock = MagicMock()
        bottle_mock.name = "dev-abc"

        backend_mock = MagicMock()
        backend_mock.prelaunch_checks.side_effect = StaleImageError("image is 5 day(s) old")
        backend_mock.launch.return_value = _ok_cm(bottle_mock)

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "read_tty_line", return_value="YES"), \
             patch.object(start_mod, "attach_agent", return_value=0), \
             patch.object(start_mod, "capture_claude_session_state"), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = self._run_launch(assume_yes=False)

        self.assertEqual(0, rc)
        backend_mock.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
