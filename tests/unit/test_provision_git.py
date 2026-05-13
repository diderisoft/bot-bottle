"""Unit: render of ~/.gitconfig pushInsteadOf rules (PRD 0008)."""

import unittest

from claude_bottle.backend.docker.provision.git import render_git_gate_gitconfig
from tests.fixtures import fixture_minimal, fixture_with_git


class TestGitGateGitconfigRender(unittest.TestCase):
    def test_empty_entries_renders_nothing(self):
        bottle = fixture_minimal().bottles["dev"]
        self.assertEqual("", render_git_gate_gitconfig("demo", bottle.git))

    def test_one_block_per_entry(self):
        bottle = fixture_with_git().bottles["dev"]
        out = render_git_gate_gitconfig("demo", bottle.git)
        # Both entries map to a [url ...] block keyed on the gate's
        # container hostname (claude-bottle-git-gate-<slug>).
        self.assertIn(
            '[url "git://claude-bottle-git-gate-demo/claude-bottle.git"]',
            out,
        )
        self.assertIn(
            "\tinsteadOf = "
            "ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
            out,
        )
        self.assertIn('[url "git://claude-bottle-git-gate-demo/foo.git"]', out)
        self.assertIn(
            "\tinsteadOf = ssh://git@github.com/didericis/foo.git",
            out,
        )

    def test_insteadOf_not_pushInsteadOf(self):
        # The gate mirrors fetch and push, so insteadOf (which rewrites
        # both directions) is the right knob. pushInsteadOf would only
        # gate push and leave fetch on the original URL — exactly the
        # v1 design we've moved past.
        bottle = fixture_with_git().bottles["dev"]
        out = render_git_gate_gitconfig("demo", bottle.git)
        self.assertIn("\tinsteadOf", out)
        self.assertNotIn("pushInsteadOf", out)


if __name__ == "__main__":
    unittest.main()
