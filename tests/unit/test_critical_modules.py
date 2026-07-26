"""Tests for the fail-closed critical coverage manifest."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.critical_modules import (
    DEFAULT_MANIFEST,
    REPO_ROOT,
    CriticalModulesError,
    load_critical_modules,
    main,
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

    def test_invalid_entry_forms_are_reported_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "critical-modules.txt"
            manifest.write_text(
                f"{root / 'absolute.py'}\nREADME.md\n",
                encoding="utf-8",
            )
            with self.assertRaises(CriticalModulesError) as raised:
                load_critical_modules(manifest, root=root)
            self.assertIn("must be relative", str(raised.exception))
            self.assertIn("is not a Python module", str(raised.exception))

    def test_empty_manifest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "critical-modules.txt"
            manifest.write_text("# comments do not define modules\n", encoding="utf-8")
            with self.assertRaisesRegex(CriticalModulesError, "contains no"):
                load_critical_modules(manifest, root=root)

    def test_unreadable_manifest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.txt"
            with self.assertRaisesRegex(CriticalModulesError, "cannot read"):
                load_critical_modules(missing, root=Path(tmp))

    def test_main_prints_include_list_or_checks_silently(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main([]))
        self.assertIn("bot_bottle/manifest/egress.py", output.getvalue())

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["--check"]))
        self.assertEqual("", output.getvalue())

    def test_main_reports_manifest_error(self) -> None:
        error = StringIO()
        with redirect_stderr(error):
            self.assertEqual(1, main(["--manifest", "/definitely/missing"]))
        self.assertIn("critical-modules:", error.getvalue())


if __name__ == "__main__":
    unittest.main()
