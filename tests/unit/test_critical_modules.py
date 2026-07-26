"""Tests for the fail-closed critical coverage manifest."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.critical_modules import (
    DEFAULT_MANIFEST,
    REPO_ROOT,
    CriticalModulesError,
    load_critical_modules,
)


class TestCriticalModules(unittest.TestCase):
    def test_repository_manifest_is_valid(self) -> None:
        modules = load_critical_modules(DEFAULT_MANIFEST, root=REPO_ROOT)
        self.assertGreater(len(modules), 20)
        self.assertEqual(len(modules), len(set(modules)))

    def test_missing_module_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "critical-modules.txt"
            manifest.write_text("bot_bottle/renamed.py\n", encoding="utf-8")
            with self.assertRaisesRegex(CriticalModulesError, "does not exist"):
                load_critical_modules(manifest, root=root)

    def test_duplicate_module_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "bot_bottle" / "core.py"
            module.parent.mkdir()
            module.write_text("", encoding="utf-8")
            manifest = root / "critical-modules.txt"
            manifest.write_text(
                "bot_bottle/core.py\nbot_bottle/core.py\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(CriticalModulesError, "duplicated"):
                load_critical_modules(manifest, root=root)

    def test_entry_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "critical-modules.txt"
            manifest.write_text("../outside.py\n", encoding="utf-8")
            with self.assertRaisesRegex(CriticalModulesError, "escapes"):
                load_critical_modules(manifest, root=root)


if __name__ == "__main__":
    unittest.main()
