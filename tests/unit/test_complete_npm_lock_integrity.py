"""Unit tests for npm's duplicate lock-entry integrity completion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.complete_npm_lock_integrity import complete_lock


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


if __name__ == "__main__":
    unittest.main()
