"""Unit: pyproject.toml packaging contract.

Guards the install/distribution surface: the console-script entry point,
the stdlib-only (empty) dependency list, and that every package-data glob
still points at a file that exists (so an installed wheel isn't missing a
Dockerfile or entrypoint the runtime reads).
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


class TestPyproject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with PYPROJECT.open("rb") as fh:
            cls.data = tomllib.load(fh)

    def test_entry_point_targets_cli_main(self):
        scripts = self.data["project"]["scripts"]
        self.assertEqual("bot_bottle.cli:main", scripts["bot-bottle"])

    def test_no_runtime_dependencies(self):
        # AGENTS.md: the package has no runtime pip dependencies.
        self.assertEqual([], self.data["project"]["dependencies"])

    def test_requires_python_311(self):
        self.assertEqual(">=3.11", self.data["project"]["requires-python"])

    def test_package_data_files_exist(self):
        pkg_data = self.data["tool"]["setuptools"]["package-data"]["bot_bottle"]
        self.assertTrue(pkg_data, "expected package-data entries")
        for rel in pkg_data:
            path = REPO_ROOT / "bot_bottle" / rel
            self.assertTrue(path.is_file(), f"package-data missing: {path}")


if __name__ == "__main__":
    unittest.main()
