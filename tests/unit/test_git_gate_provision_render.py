"""Unit: consolidated per-bottle git-gate provisioning render (PRD 0070)."""

from __future__ import annotations

import unittest

from bot_bottle.control.git_gate_render import (
    GitGateUpstream,
    git_gate_render_provision,
)


def _ups(*names: str) -> tuple[GitGateUpstream, ...]:
    return tuple(
        GitGateUpstream(
            name=n,
            upstream_url=f"ssh://git@github.com/x/{n}.git",
            upstream_host="github.com",
            upstream_port="22",
            identity_file="",
            known_host_key="",
        )
        for n in names
    )


class TestProvisionRender(unittest.TestCase):
    def test_namespaces_repos_and_creds_by_bottle_id(self) -> None:
        script = git_gate_render_provision("bottleab12", _ups("foo"))
        self.assertIn("repo=/git/bottleab12/${name}.git", script)
        self.assertIn("keyfile=/git-gate/creds/bottleab12/${name}-key", script)
        self.assertIn("mkdir -p /git/bottleab12", script)

    def test_one_init_repo_call_per_upstream(self) -> None:
        script = git_gate_render_provision("b1", _ups("foo", "bar"))
        calls = [l for l in script.splitlines() if l.startswith("init_repo ")]
        self.assertEqual(2, len(calls))

    def test_provision_does_not_start_the_daemon(self) -> None:
        # The shared gateway already serves; provisioning is init-only.
        self.assertNotIn("git daemon", git_gate_render_provision("b1", _ups("foo")))

    def test_installs_pre_receive_hook(self) -> None:
        script = git_gate_render_provision("b1", _ups("foo"))
        self.assertIn("install -m 755 /etc/git-gate/pre-receive", script)

    def test_rejects_unsafe_bottle_id(self) -> None:
        for bad in ("../etc", "a/b", "a b", "a;rm", ""):
            with self.assertRaises(ValueError):
                git_gate_render_provision(bad, _ups("foo"))


if __name__ == "__main__":
    unittest.main()
