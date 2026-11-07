"""Unit tests for trailer_check pure functions using an injectable git callable.

These tests cover the pure-function parts of the git-gate trailer check
(offenders, rebase_base, fix_command, read_baseline, check) without
spawning a subprocess or touching the filesystem beyond a small temp file for
the baseline path that check() must read.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from bot_bottle.contract.git_trailers import TRAILER_KEY
from bot_bottle.gateway.git_gate.trailer_check import (
    ACCEPT,
    MISCONFIGURED,
    REFUSE,
    Offender,
    check,
    fix_command,
    offenders,
    read_baseline,
    rebase_base,
)

_EXPECTED = "my-bottle/abc123"
_SHA = "a" * 40
_SHA2 = "b" * 40

# --- read_baseline -----------------------------------------------------------


class TestReadBaseline(unittest.TestCase):
    def _write(self, text: str) -> str:
        fd, path = tempfile.mkstemp()
        os.write(fd, text.encode())
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def test_parses_valid_40_char_shas(self) -> None:
        path = self._write(f"{_SHA}\n{_SHA2}\n")
        result = read_baseline(path)
        self.assertEqual([_SHA, _SHA2], result)

    def test_ignores_blank_and_junk_lines(self) -> None:
        path = self._write(f"\n# comment\n{_SHA}\njunk\n")
        result = read_baseline(path)
        self.assertEqual([_SHA], result)

    def test_returns_empty_list_for_blank_file(self) -> None:
        path = self._write("")
        self.assertEqual([], read_baseline(path))

    def test_returns_none_for_missing_file(self) -> None:
        self.assertIsNone(read_baseline("/does/not/exist/baseline.txt"))

    def test_accepts_64_char_sha256(self) -> None:
        sha256 = "c" * 64
        path = self._write(sha256 + "\n")
        self.assertEqual([sha256], read_baseline(path))


# --- fix_command -------------------------------------------------------------


class TestFixCommand(unittest.TestCase):
    def test_includes_expected_trailer_value(self) -> None:
        cmd = fix_command(_EXPECTED, "abc123def456")
        self.assertIn(_EXPECTED, cmd)
        self.assertIn(TRAILER_KEY, cmd)

    def test_includes_rebase_base(self) -> None:
        base = "abc123def456"
        cmd = fix_command(_EXPECTED, base)
        self.assertIn(base, cmd)

    def test_root_flag_is_passed_through(self) -> None:
        cmd = fix_command(_EXPECTED, "--root")
        self.assertIn("--root", cmd)


# --- offenders ---------------------------------------------------------------

# Record format for the git log --format= template used by offenders():
#   <sha>\x1f<subject>\x1f<trailer-values-sep-\x1d>\x1e
def _record(sha: str, subject: str, *trailers: str) -> str:
    values = "\x1d".join(trailers)
    return f"{sha}\x1f{subject}\x1f{values}\x1e"


def _fake_log(output: str):
    """Return a git callable that yields `output` for 'git log' calls."""
    def git(*args: str, stdin: str = "") -> str:
        if args[0] == "log":
            return output
        if args[0] == "for-each-ref":
            return ""
        return ""
    return git


class TestOffenders(unittest.TestCase):
    def test_missing_trailer_is_an_offender(self) -> None:
        git = _fake_log(_record(_SHA, "My commit"))
        result = offenders(git, _SHA, _EXPECTED, excluded="")
        self.assertEqual(1, len(result))
        self.assertEqual(_SHA, result[0].sha)
        self.assertEqual((), result[0].found)

    def test_wrong_trailer_value_is_an_offender(self) -> None:
        git = _fake_log(_record(_SHA, "My commit", "other-bottle/xyz"))
        result = offenders(git, _SHA, _EXPECTED, excluded="")
        self.assertEqual(1, len(result))
        self.assertIn("other-bottle/xyz", result[0].found)

    def test_correct_trailer_is_not_an_offender(self) -> None:
        git = _fake_log(_record(_SHA, "My commit", _EXPECTED))
        result = offenders(git, _SHA, _EXPECTED, excluded="")
        self.assertEqual([], result)

    def test_empty_log_output_is_clean(self) -> None:
        git = _fake_log("")
        self.assertEqual([], offenders(git, _SHA, _EXPECTED, excluded=""))

    def test_multiple_commits_all_bad(self) -> None:
        output = _record(_SHA, "first") + _record(_SHA2, "second")
        git = _fake_log(output)
        result = offenders(git, _SHA, _EXPECTED, excluded="")
        self.assertEqual(2, len(result))

    def test_mixed_good_and_bad(self) -> None:
        output = _record(_SHA, "good commit", _EXPECTED) + _record(_SHA2, "bad commit")
        git = _fake_log(output)
        result = offenders(git, _SHA, _EXPECTED, excluded="")
        self.assertEqual(1, len(result))
        self.assertEqual(_SHA2, result[0].sha)

    def test_offender_describe_no_trailer(self) -> None:
        o = Offender(sha=_SHA, subject="My commit", found=())
        desc = o.describe()
        self.assertIn("no " + TRAILER_KEY + " trailer", desc)
        self.assertIn(_SHA[:12], desc)

    def test_offender_describe_wrong_trailer(self) -> None:
        o = Offender(sha=_SHA, subject="My commit", found=("wrong-bottle/xyz",))
        desc = o.describe()
        self.assertIn("names", desc)
        self.assertIn("wrong-bottle/xyz", desc)


# --- rebase_base -------------------------------------------------------------


class TestRebaseBase(unittest.TestCase):
    def _git_revlist(self, output: str):
        def git(*args: str, stdin: str = "") -> str:
            if args[0] == "rev-list":
                return output
            return ""
        return git

    def test_single_boundary_returns_short_sha(self) -> None:
        boundary_sha = "d" * 40
        git = self._git_revlist(f"{_SHA}\n-{boundary_sha}\n")
        result = rebase_base(git, _SHA)
        self.assertEqual(boundary_sha[:12], result)

    def test_no_boundary_returns_root_flag(self) -> None:
        git = self._git_revlist(f"{_SHA}\n{_SHA2}\n")
        result = rebase_base(git, _SHA)
        self.assertEqual("--root", result)

    def test_multiple_boundaries_returns_placeholder(self) -> None:
        boundary1 = "d" * 40
        boundary2 = "e" * 40
        git = self._git_revlist(f"{_SHA}\n-{boundary1}\n-{boundary2}\n")
        result = rebase_base(git, _SHA)
        self.assertIn("<upstream branch", result)

    def test_git_failure_returns_placeholder(self) -> None:
        def git(*args: str, stdin: str = "") -> str:
            raise subprocess.CalledProcessError(128, "git")
        result = rebase_base(git, _SHA)
        self.assertIn("<upstream branch", result)


# --- check -------------------------------------------------------------------


class _FakeGitForCheck:
    """Injectable git callable with configurable responses for check()."""

    def __init__(
        self,
        *,
        mode: str = "require",
        trailer: str = _EXPECTED,
        baseline_shas: list[str] | None = None,
        tips: str = "",
        log_output: str = "",
    ) -> None:
        # Write baseline to a temp file; check() calls read_baseline(path)
        fd, self._baseline_path = tempfile.mkstemp(suffix=".baseline")
        content = "\n".join(baseline_shas or [])
        os.write(fd, content.encode())
        os.close(fd)
        self._mode = mode
        self._trailer = trailer
        self._tips = tips
        self._log_output = log_output

    def cleanup(self) -> None:
        os.unlink(self._baseline_path)

    def __call__(self, *args: str, stdin: str = "") -> str:
        if args[:2] == ("config", "--get"):
            key = args[2]
            if key == "git-gate.trailers":
                if self._mode:
                    return self._mode + "\n"
                raise subprocess.CalledProcessError(1, "git config")
            if key == "git-gate.trailer":
                if self._trailer:
                    return self._trailer + "\n"
                raise subprocess.CalledProcessError(1, "git config")
            if key == "git-gate.baseline":
                return self._baseline_path + "\n"
            raise subprocess.CalledProcessError(1, "git config")
        if args[0] == "for-each-ref":
            return self._tips
        if args[0] == "log":
            return self._log_output
        return ""


class TestCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.errors: list[str] = []

    def _err(self, msg: str) -> None:
        self.errors.append(msg)

    def _refs(self, new: str = _SHA) -> list[tuple[str, str, str]]:
        return [("0" * 40, new, "refs/heads/main")]

    def test_accepts_all_commits_with_correct_trailers(self) -> None:
        log = _record(_SHA, "good commit", _EXPECTED)
        git = _FakeGitForCheck(log_output=log)
        self.addCleanup(git.cleanup)

        result = check(self._refs(), git=git, err=self._err)

        self.assertEqual(ACCEPT, result)
        self.assertEqual([], self.errors)

    def test_refuses_in_require_mode_when_trailer_is_missing(self) -> None:
        log = _record(_SHA, "bad commit")
        git = _FakeGitForCheck(mode="require", log_output=log)
        self.addCleanup(git.cleanup)

        result = check(self._refs(), git=git, err=self._err)

        self.assertEqual(REFUSE, result)
        self.assertTrue(any("refusing" in e for e in self.errors))

    def test_accepts_in_warn_mode_despite_missing_trailer(self) -> None:
        log = _record(_SHA, "bad commit")
        git = _FakeGitForCheck(mode="warn", log_output=log)
        self.addCleanup(git.cleanup)

        result = check(self._refs(), git=git, err=self._err)

        self.assertEqual(ACCEPT, result)
        self.assertTrue(any("warning" in e for e in self.errors))

    def test_accepts_immediately_in_off_mode(self) -> None:
        log = _record(_SHA, "bad commit")
        git = _FakeGitForCheck(mode="off", log_output=log)
        self.addCleanup(git.cleanup)

        result = check(self._refs(), git=git, err=self._err)

        self.assertEqual(ACCEPT, result)
        self.assertEqual([], self.errors)

    def test_skips_deletion_refs(self) -> None:
        log = _record(_SHA, "bad commit")
        git = _FakeGitForCheck(log_output=log)
        self.addCleanup(git.cleanup)
        deletion_refs = [("0" * 40, "0" * 40, "refs/heads/deleted")]

        result = check(deletion_refs, git=git, err=self._err)

        self.assertEqual(ACCEPT, result)

    def test_misconfigured_when_trailers_config_is_missing(self) -> None:
        git = _FakeGitForCheck(mode="")
        self.addCleanup(git.cleanup)

        result = check(self._refs(), git=git, err=self._err)

        self.assertEqual(MISCONFIGURED, result)
        self.assertTrue(any("git-gate.trailers" in e for e in self.errors))

    def test_misconfigured_when_trailer_value_config_is_missing(self) -> None:
        git = _FakeGitForCheck(trailer="")
        self.addCleanup(git.cleanup)

        result = check(self._refs(), git=git, err=self._err)

        self.assertEqual(MISCONFIGURED, result)
        self.assertTrue(any("git-gate.trailer" in e for e in self.errors))

    def test_fix_command_is_included_in_error_output(self) -> None:
        log = _record(_SHA, "bad commit")
        git = _FakeGitForCheck(mode="require", log_output=log)
        self.addCleanup(git.cleanup)

        check(self._refs(), git=git, err=self._err)

        fix = "\n".join(self.errors)
        self.assertIn(TRAILER_KEY, fix)
        self.assertIn(_EXPECTED, fix)


if __name__ == "__main__":
    unittest.main()
