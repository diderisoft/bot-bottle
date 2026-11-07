"""A release's version: the first free `MAJOR.MINOR.PATCH` from a start, and
the versions the repo's git tags have taken."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from bot_bottle import release_version as rv


class NextFree(unittest.TestCase):
    def test_bumps_the_patch_past_taken_versions(self) -> None:
        self.assertEqual("0.1.4", rv.next_free("0.1.2", {"0.1.2", "0.1.3"}.__contains__))

    def test_a_free_start_is_kept(self) -> None:
        self.assertEqual("1.2.0", rv.next_free("1.2.0", lambda _v: False))

    def test_a_non_release_version_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            rv.next_free("0.0.0-dev", lambda _v: False)


class GitReleaseTags(unittest.TestCase):
    def test_only_release_tags_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            for tag in ("v0.1.6", "v0.1.7", "v1.0.0-rc1", "va.b.c", "0.2.0"):
                subprocess.run([*git, "tag", tag], check=True)
            self.assertEqual(frozenset({"0.1.6", "0.1.7"}), rv.git_release_tags(Path(tmp)))

    def test_not_a_repo_is_no_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(frozenset(), rv.git_release_tags(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
