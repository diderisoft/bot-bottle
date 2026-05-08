#!/usr/bin/env python3
"""Test runner. Wraps unittest's discovery so we can split unit /
integration the same way the bash runner did.

Usage:
  tests/run_tests.py             # unit + integration
  tests/run_tests.py unit        # unit only (no docker)
  tests/run_tests.py integration # integration only (need docker)
  tests/run_tests.py tests/test_x.py  # one specific file (or path)

Tests are auto-classified as integration when their filename matches
one of INTEGRATION_NAMES below; everything else is a unit test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

INTEGRATION_NAMES = {
    "test_dry_run_plan.py",
    "test_orphan_cleanup.py",
    "test_pipelock_image.py",
    "test_pipelock_sidecar_smoke.py",
}


def _all_test_files() -> list[Path]:
    return sorted(TESTS_DIR.glob("test_*.py"))


def _classify(path: Path) -> str:
    return "integration" if path.name in INTEGRATION_NAMES else "unit"


def _modname(path: Path) -> str:
    return f"tests.{path.stem}"


def _build_suite(files: list[Path]) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for f in files:
        suite.addTests(loader.loadTestsFromName(_modname(f)))
    return suite


def usage() -> None:
    sys.stderr.write(
        "usage: tests/run_tests.py [unit|integration|path/to/test.py]\n"
    )


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(REPO_ROOT))

    if not argv:
        files = _all_test_files()
    else:
        arg = argv[0]
        if arg in ("-h", "--help"):
            usage()
            return 0
        if arg == "unit":
            files = [f for f in _all_test_files() if _classify(f) == "unit"]
        elif arg == "integration":
            files = [f for f in _all_test_files() if _classify(f) == "integration"]
        else:
            p = Path(arg).resolve()
            if not p.is_file():
                sys.stderr.write(f"no such file: {arg}\n")
                usage()
                return 2
            files = [p]

    if not files:
        sys.stderr.write("no test files found\n")
        return 2

    suite = _build_suite(files)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
