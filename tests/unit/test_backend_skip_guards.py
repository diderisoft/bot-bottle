"""Tests for the backend-aware skip guards in ``tests/_backend.py``.

The guards delegate their readiness check to
``bot_bottle.backend.is_backend_ready`` (the probe behind ``./cli.py backend
status``); here that probe is mocked so the unit job asserts the
selection/skip logic without either backend present on the runner.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests._backend import (
    selected_backend,
    skip_unless_backend,
    skip_unless_selected_backend_available,
)


def _skipped(decorated: type) -> bool:
    return getattr(decorated, "__unittest_skip__", False)


def _new_case() -> type:
    return type("Case", (unittest.TestCase,), {})


class TestSelectedBackend(unittest.TestCase):
    def test_defaults_to_docker_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("docker", selected_backend())

    def test_reads_env(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True):
            self.assertEqual("firecracker", selected_backend())


class TestSkipUnlessBackend(unittest.TestCase):
    def test_skips_when_other_backend_selected(self):
        # A different backend is selected — no host probe needed, skip.
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True), \
                patch("tests._backend.is_backend_ready") as has:
            decorated = skip_unless_backend("docker")(_new_case())
        self.assertTrue(_skipped(decorated))
        has.assert_not_called()

    def test_runs_when_selected_and_available(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "docker"}, clear=True), \
                patch("tests._backend.is_backend_ready", return_value=True):
            decorated = skip_unless_backend("docker")(_new_case())
        self.assertFalse(_skipped(decorated))

    def test_skips_when_selected_but_unavailable(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "docker"}, clear=True), \
                patch("tests._backend.is_backend_ready", return_value=False):
            decorated = skip_unless_backend("docker")(_new_case())
        self.assertTrue(_skipped(decorated))


class TestSkipUnlessSelectedBackendAvailable(unittest.TestCase):
    def test_runs_when_selected_backend_available(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True), \
                patch("tests._backend.is_backend_ready", return_value=True) as has:
            decorated = skip_unless_selected_backend_available()(_new_case())
        self.assertFalse(_skipped(decorated))
        has.assert_called_once_with("firecracker", quiet=False)

    def test_skips_when_selected_backend_unavailable(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True), \
                patch("tests._backend.is_backend_ready", return_value=False):
            decorated = skip_unless_selected_backend_available()(_new_case())
        self.assertTrue(_skipped(decorated))


if __name__ == "__main__":
    unittest.main()
