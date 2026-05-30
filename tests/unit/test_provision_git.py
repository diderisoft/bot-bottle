"""Unit: render of ~/.gitconfig insteadOf rules (PRD 0008).

The render moved to `bot_bottle.git_gate` so both backends
share it; tests live here because docker's provision_git is the
original consumer."""

import unittest

from bot_bottle.git_gate import (
    GIT_GATE_HOSTNAME,
    git_gate_render_gitconfig,
)
from bot_bottle.manifest import Manifest
from tests.fixtures import fixture_minimal, fixture_with_git


class TestGitGateGitconfigRender(unittest.TestCase):
    def test_empty_entries_renders_nothing(self):
        bottle = fixture_minimal().bottles["dev"]
        self.assertEqual(
            "", git_gate_render_gitconfig(bottle.git, GIT_GATE_HOSTNAME),
        )

    def test_one_block_per_entry(self):
        bottle = fixture_with_git().bottles["dev"]
        out = git_gate_render_gitconfig(bottle.git, GIT_GATE_HOSTNAME)
        # Both entries map to a [url ...] block keyed on the gate's
        # short network alias (`git-gate`) inside the sidecar bundle.
        self.assertIn(
            '[url "git://git-gate/bot-bottle.git"]',
            out,
        )
        self.assertIn(
            "\tinsteadOf = "
            "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
            out,
        )
        self.assertIn('[url "git://git-gate/foo.git"]', out)
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
        out = git_gate_render_gitconfig(bottle.git, GIT_GATE_HOSTNAME)
        self.assertIn("\tinsteadOf", out)
        self.assertNotIn("pushInsteadOf", out)

    def test_gate_host_can_be_ip_port_form(self):
        # The smolmachines backend's TSI-allowlisted guest has no
        # DNS, so it dials git-gate via `<bundle_ip>:<port>`.
        bottle = fixture_with_git().bottles["dev"]
        out = git_gate_render_gitconfig(bottle.git, "192.168.20.2:9418")
        self.assertIn(
            '[url "git://192.168.20.2:9418/bot-bottle.git"]', out,
        )

    def test_scheme_can_be_http_for_smolmachines(self):
        bottle = fixture_with_git().bottles["dev"]
        out = git_gate_render_gitconfig(
            bottle.git, "127.0.0.16:57001", scheme="http",
        )
        self.assertIn(
            '[url "http://127.0.0.16:57001/bot-bottle.git"]', out,
        )

    def test_ip_upstream_also_rewrites_logical_remote_key(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {"git": {"remotes": {
                "gitea.dideric.is": {
                    "Name": "bot-bottle",
                    "Upstream": "ssh://git@192.0.2.10:30009/didericis/bot-bottle.git",
                    "IdentityFile": "/dev/null",
                },
            }}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        out = git_gate_render_gitconfig(m.bottles["dev"].git, GIT_GATE_HOSTNAME)
        self.assertIn(
            "\tinsteadOf = "
            "ssh://git@192.0.2.10:30009/didericis/bot-bottle.git",
            out,
        )
        self.assertIn(
            "\tinsteadOf = "
            "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
            out,
        )


if __name__ == "__main__":
    unittest.main()
