"""Bottle versions (PRD 0103): host bottle and agent files are versioned in a
git repo bot-bottle keeps in the root; a project bottle's history is the
project's own."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bot_bottle.manifest import versions
from bot_bottle.manifest.versions import (
    SecretInManifestError, VersionError, history, is_dirty, launch_versions, record,
    record_hand_edits, revert, show,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.root = Path(tmp) / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)
        (self.root / "agents").mkdir()
        self.bottle = self.root / "bottles" / "dev.md"

    def write(self, text: str) -> None:
        self.bottle.write_text(text)


class TestRecord(_Root):
    def test_versions_only_agents_and_bottles(self) -> None:
        self.write("---\nextends: basic\n---\n")
        (self.root / "secrets").write_text("TOKEN=abcdefghijkl\n")
        (self.root / "state").mkdir()
        (self.root / "state" / "x.json").write_text("{}")
        sha = record("first", root=self.root, secrets_file=self.root / "nope")
        self.assertTrue(sha)
        files = _git(self.root, "ls-files").split()
        self.assertEqual([".gitignore", "bottles/dev.md"], files)
        self.assertEqual("", _git(self.root, "status", "--porcelain").strip())

    def test_nothing_changed_is_the_same_version(self) -> None:
        self.write("a\n")
        first = record("first", root=self.root)
        self.assertEqual(first, record("again", root=self.root))
        self.assertEqual(1, len(history(self.bottle, root=self.root)))

    def test_hand_edits_become_a_version(self) -> None:
        self.write("a\n")
        record("first", root=self.root)
        self.write("b\n")
        record_hand_edits(root=self.root)
        self.assertEqual(["edited by hand", "first"],
                         [v.message for v in history(self.bottle, root=self.root)])

    def test_a_secret_value_is_refused(self) -> None:
        secrets = self.root / "secrets"
        secrets.write_text("GITEA_TOKEN=s3cr3t-value-123\n")
        secrets.chmod(0o600)
        self.write("env:\n  T: s3cr3t-value-123\n")
        with self.assertRaises(SecretInManifestError) as e:
            record("leak", root=self.root, secrets_file=secrets)
        self.assertIn("GITEA_TOKEN", str(e.exception))
        self.assertNotIn("s3cr3t", str(e.exception))
        self.assertEqual("", _git(self.root, "diff", "--cached").strip())  # unstaged again

    def test_users_git_environment_stays_out(self) -> None:
        self.write("a\n")
        elsewhere = Path(self.root.parent) / "other"
        elsewhere.mkdir()
        _git(elsewhere, "init", "-q")
        os.environ["GIT_DIR"] = str(elsewhere / ".git")
        self.addCleanup(os.environ.pop, "GIT_DIR")
        record("first", root=self.root)
        self.assertTrue((self.root / ".git").is_dir())


class TestHistoryAndRevert(_Root):
    def test_show_and_revert_make_a_new_version(self) -> None:
        self.write("one\n")
        first = record("first", root=self.root)
        self.write("two\n")
        record("second", root=self.root)
        self.assertIn("+two", show(self.bottle, history(self.bottle, root=self.root)[0].sha[:9],
                                   root=self.root))
        sha = revert(self.bottle, first[:9], root=self.root)
        self.assertEqual("one\n", self.bottle.read_text())
        self.assertEqual(f"revert bottles/dev.md to {first[:9]}",
                         history(self.bottle, root=self.root)[0].message)
        self.assertEqual(sha, history(self.bottle, root=self.root)[0].sha)
        self.assertEqual(3, len(history(self.bottle, root=self.root)))

    def test_revert_keeps_a_hand_edit_in_history(self) -> None:
        self.write("one\n")
        first = record("first", root=self.root)
        self.write("edited\n")
        revert(self.bottle, first, root=self.root)
        messages = [v.message for v in history(self.bottle, root=self.root)]
        self.assertEqual("edited by hand", messages[1])

    def test_unknown_version(self) -> None:
        self.write("one\n")
        record("first", root=self.root)
        with self.assertRaises(VersionError):
            revert(self.bottle, "deadbeef", root=self.root)
        with self.assertRaises(VersionError):
            show(self.bottle, "", root=self.root)


class TestProjectBottles(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.project = Path(self.root.parent) / "proj"
        (self.project / ".bot-bottle" / "bottles").mkdir(parents=True)
        _git(self.project, "init", "-q")
        _git(self.project, "config", "user.email", "t@example.com")
        _git(self.project, "config", "user.name", "t")
        self.main = self.project / ".bot-bottle" / "bottles" / "main.md"
        self.main.write_text("one\n")
        _git(self.project, "add", "-A")
        _git(self.project, "commit", "-qm", "add bottle")
        self.first = _git(self.project, "rev-parse", "HEAD").strip()
        self.main.write_text("two\n")
        _git(self.project, "commit", "-qam", "widen it")

    def test_history_is_the_projects(self) -> None:
        self.assertEqual(["widen it", "add bottle"],
                         [v.message for v in history(self.main, root=self.root)])

    def test_revert_is_left_for_you_to_commit(self) -> None:
        self.assertEqual("", revert(self.main, self.first, root=self.root))
        self.assertEqual("one\n", self.main.read_text())
        self.assertTrue(is_dirty(self.main, root=self.root))
        self.assertEqual(2, len(history(self.main, root=self.root)))

    def test_launch_records_both(self) -> None:
        self.write("a\n")
        out = launch_versions(str(self.project), root=self.root)
        self.assertEqual(_git(self.root, "rev-parse", "HEAD").strip(), out["host"])
        self.assertEqual(_git(self.project, "rev-parse", "HEAD").strip(), out["project"])
        self.assertFalse(out["project_dirty"])
        self.main.write_text("three\n")
        self.assertTrue(launch_versions(str(self.project), root=self.root)["project_dirty"])

    def test_launch_without_a_project(self) -> None:
        self.assertEqual({"host"}, set(launch_versions(str(self.root.parent), root=self.root)))

    def test_launch_survives_a_refused_commit(self) -> None:
        versions_record = versions.record

        def refuse(*_a: object, **_k: object) -> str:
            raise VersionError("refused")
        versions.record = refuse  # type: ignore[assignment]
        self.addCleanup(setattr, versions, "record", versions_record)
        out = launch_versions(str(self.project), root=self.root)
        self.assertEqual("refused", out["host_error"])


if __name__ == "__main__":
    unittest.main()
