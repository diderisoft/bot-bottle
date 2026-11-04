"""publish_images.default_jobs: how many image builds run at once.

Kept out of test_publish_images.py, which is skipped in CI (#749)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from bot_bottle import publish_images as pi


class TestDefaultJobs(unittest.TestCase):
    def _jobs_with(self, meminfo: str, cpus: int) -> int:
        with tempfile.NamedTemporaryFile("w", suffix="meminfo", delete=False) as f:
            f.write(meminfo)
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(pi.os, "cpu_count", return_value=cpus):
            return pi.default_jobs(f.name)

    def test_held_to_one_build_per_6_gib_available(self) -> None:
        """24 cores alone would allow 6 builds; 13 GiB available allows 2."""
        meminfo = "MemTotal:       32785368 kB\nMemAvailable:   13631488 kB\n"
        self.assertEqual(self._jobs_with(meminfo, 24), 2)

    def test_never_below_one(self) -> None:
        self.assertEqual(self._jobs_with("MemAvailable:     1048576 kB\n", 24), 1)

    def test_cores_decide_when_memory_allows(self) -> None:
        self.assertEqual(self._jobs_with("MemAvailable:   134217728 kB\n", 16), 4)

    def test_cores_still_capped_at_8(self) -> None:
        self.assertEqual(self._jobs_with("MemAvailable:   268435456 kB\n", 64), 8)

    def test_cores_alone_without_meminfo(self) -> None:
        with mock.patch.object(pi.os, "cpu_count", return_value=24):
            self.assertEqual(pi.default_jobs("/nonexistent/meminfo"), 6)
        self.assertEqual(self._jobs_with("no such field\n", 24), 6)
        self.assertEqual(self._jobs_with("MemAvailable: lots\n", 24), 6)


if __name__ == "__main__":
    unittest.main()
