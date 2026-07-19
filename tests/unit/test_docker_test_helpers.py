"""Tests for integration-test backend selection helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests._docker import skip_unless_docker_or_firecracker


class TestSkipUnlessDockerOrFirecracker(unittest.TestCase):
    def test_firecracker_runs_when_docker_tests_are_disabled(self):
        with patch.dict(
            os.environ,
            {"BOT_BOTTLE_BACKEND": "firecracker", "SKIP_DOCKER_TESTS": "1"},
            clear=True,
        ):
            decorated = skip_unless_docker_or_firecracker()(type("Case", (), {}))

        self.assertFalse(getattr(decorated, "__unittest_skip__", False))

    def test_non_firecracker_still_skips_when_docker_tests_are_disabled(self):
        with patch.dict(
            os.environ,
            {"BOT_BOTTLE_BACKEND": "docker", "SKIP_DOCKER_TESTS": "1"},
            clear=True,
        ):
            decorated = skip_unless_docker_or_firecracker()(type("Case", (), {}))

        self.assertTrue(decorated.__unittest_skip__)


if __name__ == "__main__":
    unittest.main()
