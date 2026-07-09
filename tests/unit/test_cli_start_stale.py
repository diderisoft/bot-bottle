"""Unit: _launch_bottle StaleImageError handling.

Exercises the while-True / try-except loop around backend.launch:
  - headless mode → die on stale
  - interactive mode, user declines → stop without launching
  - interactive mode, user confirms → retry with skip_stale=True
"""

from __future__ import annotations

import io
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from bot_bottle.image_cache import StaleImageError
from bot_bottle.log import Die


def _fake_plan() -> Any:
    provision = SimpleNamespace(startup_args=())
    return cast(Any, SimpleNamespace(
        agent_provision=provision,
        agent_provider_template="claude",
        slug="dev-abc",
    ))


def _stale_cm() -> MagicMock:
    """Return a context-manager mock whose __enter__ raises StaleImageError."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(side_effect=StaleImageError("image is 5 day(s) old"))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


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
        import bot_bottle.cli.start as start_mod
        spec = self._spec()
        with patch.object(start_mod, "prepare_with_preflight",
                          return_value=(_fake_plan(), "dev-abc")), \
             patch.object(start_mod, "settle_state"), \
             patch.object(start_mod, "info"):
            return start_mod._launch_bottle(
                spec,
                dry_run=False,
                backend_name="docker",
                **patch_kwargs,
            )

    def test_headless_stale_calls_die(self) -> None:
        """In headless mode (assume_yes=True), a StaleImageError must call die()."""
        import bot_bottle.cli.start as start_mod

        backend_mock = MagicMock()
        backend_mock.launch.return_value = _stale_cm()

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "die", side_effect=Die()):
            with self.assertRaises(Die):
                self._run_launch(assume_yes=True)

    def test_interactive_user_declines_stops_loop(self) -> None:
        """Interactive user answering 'n' → loop exits without a second launch."""
        import bot_bottle.cli.start as start_mod

        backend_mock = MagicMock()
        backend_mock.launch.return_value = _stale_cm()

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "read_tty_line", return_value="n"), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = self._run_launch(assume_yes=False)

        self.assertEqual(0, rc)
        # launch was called once (stale), then the user declined → no second call.
        backend_mock.launch.assert_called_once()

    def test_interactive_user_confirms_retries_with_skip_stale(self) -> None:
        """Interactive user answering 'y' → second launch with skip_stale=True."""
        import bot_bottle.cli.start as start_mod

        bottle_mock = MagicMock()
        bottle_mock.name = "dev-abc"

        def launch_side_effect(plan: Any, *, skip_stale: bool = False) -> Any:
            if not skip_stale:
                return _stale_cm()
            return _ok_cm(bottle_mock)

        backend_mock = MagicMock()
        backend_mock.launch.side_effect = launch_side_effect

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "read_tty_line", return_value="y"), \
             patch.object(start_mod, "attach_agent", return_value=0), \
             patch.object(start_mod, "capture_claude_session_state"), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = self._run_launch(assume_yes=False)

        self.assertEqual(0, rc)
        # First call: skip_stale=False → stale. Second call: skip_stale=True → ok.
        self.assertEqual(2, backend_mock.launch.call_count)
        calls = backend_mock.launch.call_args_list
        self.assertFalse(calls[0].kwargs.get("skip_stale", False))
        self.assertTrue(calls[1].kwargs.get("skip_stale", False))

    def test_interactive_yes_uppercase_also_accepted(self) -> None:
        """'Y' or 'YES' should also be accepted as confirmation."""
        import bot_bottle.cli.start as start_mod

        bottle_mock = MagicMock()
        bottle_mock.name = "dev-abc"

        def launch_side_effect(plan: Any, *, skip_stale: bool = False) -> Any:
            if not skip_stale:
                return _stale_cm()
            return _ok_cm(bottle_mock)

        backend_mock = MagicMock()
        backend_mock.launch.side_effect = launch_side_effect

        with patch.object(start_mod, "get_bottle_backend", return_value=backend_mock), \
             patch.object(start_mod, "read_tty_line", return_value="YES"), \
             patch.object(start_mod, "attach_agent", return_value=0), \
             patch.object(start_mod, "capture_claude_session_state"), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = self._run_launch(assume_yes=False)

        self.assertEqual(0, rc)
        self.assertEqual(2, backend_mock.launch.call_count)


if __name__ == "__main__":
    unittest.main()
