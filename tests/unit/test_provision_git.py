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
            "\tpushInsteadOf = "
            "ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
            out,
        )
        self.assertIn('[url "git://claude-bottle-git-gate-demo/foo.git"]', out)
        self.assertIn(
            "\tpushInsteadOf = ssh://git@github.com/didericis/foo.git",
            out,
        )

    def test_pushInsteadOf_not_insteadOf(self):
        # insteadOf would route fetch through the gate too; v1 only
        # gates push. If this assertion ever fails we've inadvertently
        # widened the gate's scope.
        bottle = fixture_with_git().bottles["dev"]
        out = render_git_gate_gitconfig("demo", bottle.git)
        self.assertIn("pushInsteadOf", out)
        self.assertNotIn("\tinsteadOf", out)


if __name__ == "__main__":
    unittest.main()
