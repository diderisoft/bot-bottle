"""Unit tests for held_secret_scan.main() fail-closed paths.

These tests use injectable callables and unittest.mock to cover the
error/fail-closed paths without spawning a subprocess or requiring a live
git repository (the integration tests in test_held_secrets.py cover the
happy path but are marked flaky due to git environment issues).
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bot_bottle.gateway.git_gate import held_secret_scan as scan

_SHA = "a" * 40
_SECRETS = {"HELD_ENV_MY_KEY": "secretvalue-should-not-appear-in-output"}


def _write_refs(content: str, cleanup: unittest.TestCase) -> str:
    fd, path = tempfile.mkstemp(suffix=".refs")
    os.write(fd, content.encode())
    os.close(fd)
    cleanup.addCleanup(os.unlink, path)
    return path


class TestMainArgParsing(unittest.TestCase):
    def test_no_args_returns_cannot_scan(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = scan.main([], {})
        self.assertEqual(scan.CANNOT_SCAN, result)

    def test_too_many_args_returns_cannot_scan(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = scan.main(["a", "b"], {})
        self.assertEqual(scan.CANNOT_SCAN, result)


class TestMainResolvePaths(unittest.TestCase):
    def test_unresolvable_bottle_returns_cannot_scan(self) -> None:
        path = _write_refs(f"{'0' * 40} {_SHA} refs/heads/main\n", self)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = scan.main([path], {}, resolve=lambda _e: None)
        self.assertEqual(scan.CANNOT_SCAN, result)
        self.assertIn("cannot resolve", err.getvalue())

    def test_cannot_scan_does_not_leak_secret_values(self) -> None:
        path = _write_refs(f"{'0' * 40} {_SHA} refs/heads/main\n", self)
        secret_value = "super-secret-deadbeef"
        env = {"HELD_ENV_FOO": secret_value}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # Pass a resolve that returns secrets but patch find_held_secret to crash
            with patch.object(scan, "find_held_secret",
                              side_effect=subprocess.CalledProcessError(128, "git")):
                scan.main([path], env, resolve=lambda _e: env)
        self.assertNotIn(secret_value, err.getvalue())


class TestMainFailClosed(unittest.TestCase):
    """main() must return CANNOT_SCAN on any infrastructure failure."""

    def _run(self, error: Exception) -> tuple[int, str]:
        path = _write_refs(f"{'0' * 40} {_SHA} refs/heads/main\n", self)
        err = io.StringIO()
        with patch.object(scan, "find_held_secret", side_effect=error):
            with contextlib.redirect_stderr(err):
                result = scan.main([path], {}, resolve=lambda _e: _SECRETS)
        return result, err.getvalue()

    def test_git_crash_returns_cannot_scan(self) -> None:
        result, output = self._run(subprocess.CalledProcessError(128, "git"))
        self.assertEqual(scan.CANNOT_SCAN, result)
        self.assertIn("held-secret scan failed", output)
        self.assertIn("CalledProcessError", output)

    def test_oserror_returns_cannot_scan(self) -> None:
        result, output = self._run(OSError("Permission denied"))
        self.assertEqual(scan.CANNOT_SCAN, result)
        self.assertIn("held-secret scan failed", output)
        self.assertIn("OSError", output)

    def test_missing_refs_file_returns_cannot_scan(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = scan.main(["/does/not/exist/refs.txt"], {},
                               resolve=lambda _e: _SECRETS)
        self.assertEqual(scan.CANNOT_SCAN, result)
        self.assertIn("held-secret scan failed", err.getvalue())


class TestMainCleanPath(unittest.TestCase):
    """main() clean returns CLEAN and finds nothing."""

    def test_no_refs_is_clean(self) -> None:
        path = _write_refs("", self)
        result = scan.main([path], {}, resolve=lambda _e: _SECRETS)
        self.assertEqual(scan.CLEAN, result)


if __name__ == "__main__":
    unittest.main()
