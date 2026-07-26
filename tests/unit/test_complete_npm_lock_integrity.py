"""Unit tests for npm's duplicate lock-entry integrity completion."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import complete_npm_lock_integrity as integrity_tool

complete_lock = integrity_tool.complete_lock


class TestCompleteLock(unittest.TestCase):
    def _lock(self, directory: str, packages: dict[str, dict[str, str]]) -> Path:
        path = Path(directory) / "package-lock.json"
        path.write_text(json.dumps({
            "lockfileVersion": 3,
            "packages": packages,
        }))
        return path

    def test_copies_integrity_only_for_identical_resolved_url(self):
        url = "https://registry.npmjs.org/example/-/example-1.2.3.tgz"
        integrity = "sha512-trusted"
        with tempfile.TemporaryDirectory() as directory:
            path = self._lock(directory, {
                "node_modules/example": {
                    "resolved": url,
                    "integrity": integrity,
                },
                "node_modules/parent/node_modules/example": {
                    "resolved": url,
                },
            })
            self.assertEqual(1, complete_lock(path))
            packages = json.loads(path.read_text())["packages"]
        self.assertEqual(
            integrity,
            packages["node_modules/parent/node_modules/example"]["integrity"],
        )

    def test_rejects_missing_integrity_without_verified_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._lock(directory, {
                "node_modules/example": {
                    "resolved": (
                        "https://registry.npmjs.org/example/-/example-1.2.3.tgz"
                    ),
                },
            })
            with self.assertRaisesRegex(ValueError, "no verified duplicate"):
                complete_lock(path)

    def test_rejects_conflicting_integrities_for_same_url(self):
        url = "https://registry.npmjs.org/example/-/example-1.2.3.tgz"
        with tempfile.TemporaryDirectory() as directory:
            path = self._lock(directory, {
                "node_modules/a": {"resolved": url, "integrity": "sha512-a"},
                "node_modules/b": {"resolved": url, "integrity": "sha512-b"},
            })
            with self.assertRaisesRegex(ValueError, "conflicting integrity"):
                complete_lock(path)

    def test_ignores_local_entries_and_leaves_complete_lock_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._lock(directory, {
                "": {"name": "root"},
                "node_modules/local": {"resolved": "file:../local"},
                "node_modules/complete": {
                    "resolved": "https://registry.example/complete.tgz",
                    "integrity": "sha512-complete",
                },
            })
            before = path.read_text()
            self.assertEqual(0, complete_lock(path))
            self.assertEqual(before, path.read_text())

    def test_main_processes_every_requested_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self._lock(directory, {})
            second = Path(directory) / "second-lock.json"
            second.write_text(first.read_text())
            stdout = io.StringIO()
            with patch(
                "sys.argv",
                ["complete_npm_lock_integrity.py", str(first), str(second)],
            ), redirect_stdout(stdout):
                self.assertEqual(0, integrity_tool.main())
        self.assertEqual(2, stdout.getvalue().count("completed 0"))


if __name__ == "__main__":
    unittest.main()
