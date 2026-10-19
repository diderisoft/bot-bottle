"""Unit: macOS active-agent enumeration when `container list` fails."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend import EnumerationError
from bot_bottle.backend.macos_container import enumerate as menum

_FAILED = subprocess.CompletedProcess(
    ["container"], 1, "", 'Error: interrupted: "XPC connection error: Connection invalid"')


class TestEnumerateActive(unittest.TestCase):
    def test_stopped_service_means_no_bottles(self) -> None:
        # Apple Container installed but its service stopped must not break
        # cross-backend enumeration (e.g. `start` on another backend).
        with patch.object(menum.subprocess, "run", return_value=_FAILED), \
             patch.object(menum.container_mod, "service_running", return_value=False):
            self.assertEqual([], menum.enumerate_active())

    def test_failure_with_the_service_up_still_raises(self) -> None:
        with patch.object(menum.subprocess, "run", return_value=_FAILED), \
             patch.object(menum.container_mod, "service_running", return_value=True), \
             self.assertRaises(EnumerationError):
            menum.enumerate_active()


if __name__ == "__main__":
    unittest.main()
