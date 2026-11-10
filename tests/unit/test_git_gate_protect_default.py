"""Unit: git-gate's default-branch protection (#808): the manifest setting,
its plumbing to the gate repos, and the pre-receive phase run against a real
repo with the upstream stubbed."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bot_bottle.control.git_gate_protect import PROTECT_DEFAULT_BRANCH_PHASE
from bot_bottle.control.git_gate_render import (
    GitGateUpstream,
    git_gate_render_hook,
    git_gate_render_provision,
    git_gate_upstreams_for_bottle,
)
from bot_bottle.git_gate.provision import _write_upstreams_snapshot, load_upstreams_snapshot
from bot_bottle.manifest import ManifestBottle, ManifestGitEntry
from bot_bottle.manifest.document import bottle_document
from bot_bottle.manifest.extends import merge_bottles_runtime
from bot_bottle.manifest.util import ManifestError

ZERO = "0" * 40

# Stands in for the rest of pre-receive around the phase: the saved stdin,
# `audit_push`, and `upstream_git`, whose `ls-remote` answers $FAKE_HEAD and
# (like ssh) reads whatever stdin it's given.
_HARNESS = r"""#!/bin/sh
set -u
zero=0000000000000000000000000000000000000000
refs_file=$(mktemp)
cat > "$refs_file"
audit_push() { echo "AUDIT $*" >&2; }
upstream_git() {
  cat >/dev/null
  if [ "$1" = ls-remote ] && [ -n "${FAKE_HEAD:-}" ]; then
    printf 'ref: %s\tHEAD\n' "$FAKE_HEAD"
  fi
}
"""


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class TestPhase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "a")
        self.a = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "b")
        self.b = _git(self.repo, "rev-parse", "HEAD")
        # c branches off a, so a -> c is a fast-forward but b -> c isn't.
        _git(self.repo, "checkout", "-q", "--detach", self.a)
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "c")
        self.c = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "config", "remote.origin.url", "ssh://git@forge.example/o/r.git")
        self.protect(True)
        self.script = self.repo / "phase.sh"
        self.script.write_text(_HARNESS + PROTECT_DEFAULT_BRANCH_PHASE + "echo PASSED\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def protect(self, on: bool) -> None:
        _git(self.repo, "config", "git-gate.protectDefault", "true" if on else "false")

    def run_phase(self, stdin: str,
                  head: str = "refs/heads/main") -> subprocess.CompletedProcess[str]:
        return subprocess.run(["sh", str(self.script)], input=stdin, capture_output=True,
                              text=True, cwd=self.repo, check=False,
                              env={**os.environ, "FAKE_HEAD": head})

    def assert_refused(self, result: subprocess.CompletedProcess[str], why: str) -> None:
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertNotIn("PASSED", result.stdout)
        self.assertIn(why, result.stderr)

    def assert_passes(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("PASSED", result.stdout)

    def test_refuses_deleting_the_default_branch(self) -> None:
        result = self.run_phase(f"{self.b} {ZERO} refs/heads/main\n")
        self.assert_refused(result, "refusing to delete refs/heads/main")
        self.assertIn("AUDIT forge.push_rejected blocked refs/heads/main --detail "
                      "default branch protected", result.stderr)
        self.assertIn("open a pull request", result.stderr)

    def test_refuses_rewriting_the_default_branch(self) -> None:
        result = self.run_phase(f"{self.b} {self.c} refs/heads/main\n")
        self.assert_refused(result, "refusing to force-push to refs/heads/main")

    def test_fast_forward_to_the_default_branch_passes(self) -> None:
        self.assert_passes(self.run_phase(f"{self.a} {self.b} refs/heads/main\n"))

    def test_new_default_branch_passes(self) -> None:
        self.assert_passes(self.run_phase(f"{ZERO} {self.b} refs/heads/main\n"))

    def test_other_branches_can_be_rewritten_and_deleted(self) -> None:
        self.assert_passes(self.run_phase(
            f"{self.b} {self.c} refs/heads/feature\n{self.b} {ZERO} refs/heads/old\n"))

    def test_the_default_branch_is_whatever_upstream_says(self) -> None:
        self.assert_passes(self.run_phase(f"{self.b} {ZERO} refs/heads/main\n",
                                          head="refs/heads/trunk"))
        self.assert_refused(self.run_phase(f"{self.b} {ZERO} refs/heads/trunk\n",
                                           head="refs/heads/trunk"),
                            "refusing to delete refs/heads/trunk")

    def test_tags_are_not_branches(self) -> None:
        self.assert_passes(self.run_phase(f"{self.b} {ZERO} refs/tags/main\n"))

    def test_fails_closed_when_upstream_names_no_default(self) -> None:
        result = self.run_phase(f"{self.b} {ZERO} refs/heads/main\n", head="")
        self.assert_refused(result, "the upstream didn't say which branch that is")
        self.assertIn("default branch unknown", result.stderr)

    def test_unprotected_repo_allows_everything(self) -> None:
        self.protect(False)
        self.assert_passes(self.run_phase(
            f"{self.b} {ZERO} refs/heads/main\n{self.b} {self.c} refs/heads/main\n", head=""))

    def test_upstream_query_cannot_swallow_the_remaining_refs(self) -> None:
        # The first ref makes the phase ask the upstream (whose stub reads
        # stdin, as ssh does); the second must still be checked.
        result = self.run_phase(f"{self.b} {self.c} refs/heads/feature\n"
                                f"{self.b} {ZERO} refs/heads/main\n")
        self.assert_refused(result, "refusing to delete refs/heads/main")


class TestHook(unittest.TestCase):
    def test_runs_before_any_ref_is_forwarded(self) -> None:
        hook = git_gate_render_hook()
        self.assertNotIn("@PROTECT_DEFAULT_BRANCH@", hook)
        phase = hook.index("Phase 2a: with git-gate.protectDefault")
        self.assertLess(hook.index("upstream_git() "), phase)
        self.assertLess(phase, hook.index("forwarding $ref to origin"))


def _entry(name: str = "r") -> ManifestGitEntry:
    return ManifestGitEntry.from_repos_entry("b", name, {
        "url": f"ssh://git@forge.example/o/{name}.git",
        "key": {"provider": "static", "path": "/k"}})


class TestManifest(unittest.TestCase):
    def _bottle(self, git_gate: dict[str, object]) -> ManifestBottle:
        return ManifestBottle.from_dict("b", {"git_gate": git_gate})

    def test_unset_is_none_and_parses_booleans(self) -> None:
        self.assertIsNone(self._bottle({}).git_protect_default_branch)
        self.assertTrue(self._bottle({"protect_default_branch": True}).git_protect_default_branch)
        self.assertFalse(self._bottle({"protect_default_branch": False}).git_protect_default_branch)

    def test_rejects_a_non_boolean(self) -> None:
        with self.assertRaisesRegex(ManifestError, "protect_default_branch must be true or false"):
            self._bottle({"protect_default_branch": "yes"})

    def test_later_bottle_wins_when_it_sets_it(self) -> None:
        on, off, unset = (ManifestBottle(git_protect_default_branch=v) for v in (True, False, None))
        self.assertTrue(merge_bottles_runtime([on, unset]).git_protect_default_branch)
        self.assertFalse(merge_bottles_runtime([on, off]).git_protect_default_branch)
        self.assertTrue(merge_bottles_runtime([off, on]).git_protect_default_branch)

    def test_document_round_trip(self) -> None:
        doc = bottle_document(ManifestBottle(git_protect_default_branch=False))
        git_gate = doc["git_gate"]
        assert isinstance(git_gate, dict)
        self.assertIs(False, git_gate["protect_default_branch"])
        self.assertNotIn("git_gate", bottle_document(ManifestBottle()))


class TestPlumbing(unittest.TestCase):
    def test_bottle_setting_reaches_each_upstream(self) -> None:
        on = git_gate_upstreams_for_bottle(
            ManifestBottle(git=(_entry("a"), _entry("b")), git_protect_default_branch=True))
        self.assertEqual([True, True], [u.protect_default_branch for u in on])
        (off,) = git_gate_upstreams_for_bottle(ManifestBottle(git=(_entry(),)))
        self.assertFalse(off.protect_default_branch)

    def test_provision_sets_the_repo_config(self) -> None:
        up = GitGateUpstream(name="r", upstream_url="ssh://git@forge.example/o/r.git",
                             upstream_host="forge.example", upstream_port="22",
                             identity_file="/k", known_host_key="", protect_default_branch=True)
        script = git_gate_render_provision("b1", (up,), trailer_mode="", trailer="",
                                           baseline="/git-gate/creds/b1/baseline-commits")
        self.assertIn("git-gate.protectDefault \"$protect_default\"", script)
        line = next(l for l in script.splitlines() if l.startswith("init_repo "))
        self.assertTrue(line.endswith(" true"), line)

    def test_snapshot_keeps_it_for_reprovision(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d)
            up = GitGateUpstream(name="r", upstream_url="u", upstream_host="h", upstream_port="22",
                                 identity_file="", known_host_key="", protect_default_branch=True)
            _write_upstreams_snapshot(stage, (up,))
            (loaded,) = load_upstreams_snapshot(stage)
            self.assertTrue(loaded.protect_default_branch)
            # A snapshot from before the setting reads as unprotected.
            data = json.loads((stage / "upstreams.json").read_text())
            del data[0]["protect_default_branch"]
            (stage / "upstreams.json").write_text(json.dumps(data))
            (old,) = load_upstreams_snapshot(stage)
            self.assertFalse(old.protect_default_branch)


if __name__ == "__main__":
    unittest.main()
