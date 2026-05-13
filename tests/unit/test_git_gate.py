"""Unit: GitGate prepare shape + entrypoint/hook render (PRD 0008)."""

import os
import tempfile
import unittest
from pathlib import Path

from claude_bottle.git_gate import (
    GitGate,
    GitGatePlan,
    GitGateUpstream,
    git_gate_known_hosts_line,
    git_gate_render_entrypoint,
    git_gate_render_hook,
    git_gate_upstreams_for_bottle,
)
from tests.fixtures import fixture_minimal, fixture_with_git


class _StubGate(GitGate):
    def start(self, plan: GitGatePlan) -> str:
        raise NotImplementedError

    def stop(self, target: str) -> None:
        raise NotImplementedError


class TestUpstreamsForBottle(unittest.TestCase):
    def test_one_upstream_per_git_entry(self):
        bottle = fixture_with_git().bottles["dev"]
        ups = git_gate_upstreams_for_bottle(bottle)
        self.assertEqual(2, len(ups))
        self.assertEqual("claude-bottle", ups[0].name)
        self.assertEqual("gitea.dideric.is", ups[0].upstream_host)
        self.assertEqual("30009", ups[0].upstream_port)
        self.assertEqual("foo", ups[1].name)
        self.assertEqual("github.com", ups[1].upstream_host)
        self.assertEqual("22", ups[1].upstream_port)

    def test_empty_bottle_yields_empty_upstreams(self):
        bottle = fixture_minimal().bottles["dev"]
        self.assertEqual((), git_gate_upstreams_for_bottle(bottle))


class TestKnownHostsLine(unittest.TestCase):
    def test_default_port_unbracketed(self):
        line = git_gate_known_hosts_line("github.com", "22", "ssh-ed25519 AAAA")
        self.assertEqual("github.com ssh-ed25519 AAAA\n", line)

    def test_non_default_port_bracketed(self):
        line = git_gate_known_hosts_line("gitea.dideric.is", "30009", "ssh-ed25519 AAAA")
        self.assertEqual("[gitea.dideric.is]:30009 ssh-ed25519 AAAA\n", line)


class TestEntrypointRender(unittest.TestCase):
    def test_one_init_repo_call_per_upstream(self):
        ups = (
            GitGateUpstream(
                name="claude-bottle",
                upstream_url="ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
                upstream_host="gitea.dideric.is",
                upstream_port="30009",
                identity_file="/host/path/key",
                known_host_key="ssh-ed25519 AAAA",
            ),
            GitGateUpstream(
                name="foo",
                upstream_url="ssh://git@github.com/didericis/foo.git",
                upstream_host="github.com",
                upstream_port="22",
                identity_file="/host/path/key2",
                known_host_key="",
            ),
        )
        script = git_gate_render_entrypoint(ups)
        self.assertIn("#!/bin/sh", script)
        self.assertIn(
            "init_repo 'claude-bottle' "
            "'ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git'",
            script,
        )
        self.assertIn(
            "init_repo 'foo' 'ssh://git@github.com/didericis/foo.git'",
            script,
        )
        # Daemon line is what keeps PID 1 alive.
        self.assertIn("exec git daemon", script)
        self.assertIn("--enable=receive-pack", script)
        self.assertIn("--base-path=/git", script)

    def test_empty_upstreams_still_execs_daemon(self):
        # A no-upstream gate is a no-op for repos but the daemon still
        # has to start so the entrypoint doesn't exit.
        script = git_gate_render_entrypoint(())
        self.assertNotIn("init_repo '", script)
        self.assertIn("exec git daemon", script)


class TestHookRender(unittest.TestCase):
    def test_hook_has_two_phases(self):
        hook = git_gate_render_hook()
        # Phase 1: gitleaks. Phase 2: forward.
        self.assertIn("gitleaks git", hook)
        self.assertIn("git push upstream", hook)
        # KnownHostKey absence is fail-closed.
        self.assertIn("refusing to push", hook)
        # Stdin is buffered to a tempfile so both phases can re-read.
        self.assertIn("refs_file=$(mktemp)", hook)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.stage, ignore_errors=True)

    def test_prepare_writes_entrypoint_and_hook_mode_600(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        self.assertEqual(
            self.stage / "git_gate_entrypoint.sh", plan.entrypoint_script
        )
        self.assertEqual(
            self.stage / "git_gate_pre_receive.sh", plan.hook_script
        )
        self.assertEqual(0o600, os.stat(plan.entrypoint_script).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(plan.hook_script).st_mode & 0o777)

    def test_prepare_plan_carries_upstreams_and_slug(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        self.assertEqual("demo", plan.slug)
        self.assertEqual(2, len(plan.upstreams))
        self.assertEqual("", plan.internal_network)
        self.assertEqual("", plan.egress_network)

    def test_prepare_with_no_git_writes_minimal_script(self):
        plan = _StubGate().prepare(
            fixture_minimal().bottles["dev"], "demo", self.stage
        )
        self.assertEqual((), plan.upstreams)
        content = plan.entrypoint_script.read_text()
        self.assertNotIn("init_repo '", content)
        self.assertIn("exec git daemon", content)


if __name__ == "__main__":
    unittest.main()
