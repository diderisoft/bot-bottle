"""Unit: GitGate prepare shape + entrypoint/hook render (PRD 0008)."""

import os
import tempfile
import unittest
from pathlib import Path

from bot_bottle.git_gate import (
    GitGate,
    GitGatePlan,
    GitGateUpstream,
    git_gate_aggregate_extra_hosts,
    git_gate_known_hosts_line,
    git_gate_render_access_hook,
    git_gate_render_entrypoint,
    git_gate_render_hook,
    git_gate_upstreams_for_bottle,
)
from bot_bottle.log import Die
from bot_bottle.manifest import Manifest
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
        self.assertEqual("bot-bottle", ups[0].name)
        self.assertEqual("gitea.dideric.is", ups[0].upstream_host)
        self.assertEqual("30009", ups[0].upstream_port)
        self.assertEqual("foo", ups[1].name)
        self.assertEqual("github.com", ups[1].upstream_host)
        self.assertEqual("22", ups[1].upstream_port)

    def test_empty_bottle_yields_empty_upstreams(self):
        bottle = fixture_minimal().bottles["dev"]
        self.assertEqual((), git_gate_upstreams_for_bottle(bottle))


class TestExtraHostsPlumbing(unittest.TestCase):
    def test_upstream_carries_extra_hosts_from_manifest(self):
        m = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "git": {"remotes": {
                        "gitea.dideric.is": {
                            "Name": "bot-bottle",
                            "Upstream": "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
                            "IdentityFile": "/dev/null",
                            "ExtraHosts": {"gitea.dideric.is": "192.0.2.10"},
                        },
                    }},
                },
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        ups = git_gate_upstreams_for_bottle(m.bottles["dev"])
        self.assertEqual(
            {"gitea.dideric.is": "192.0.2.10"}, dict(ups[0].extra_hosts)
        )

    def test_aggregator_merges_distinct_hostnames(self):
        ups = (
            GitGateUpstream(
                name="a", upstream_url="", upstream_host="", upstream_port="",
                identity_file="", known_host_key="",
                extra_hosts={"a.example": "10.0.0.1"},
            ),
            GitGateUpstream(
                name="b", upstream_url="", upstream_host="", upstream_port="",
                identity_file="", known_host_key="",
                extra_hosts={"b.example": "10.0.0.2"},
            ),
        )
        self.assertEqual(
            {"a.example": "10.0.0.1", "b.example": "10.0.0.2"},
            git_gate_aggregate_extra_hosts(ups),
        )

    def test_aggregator_allows_same_host_same_ip(self):
        # Two entries listing the same host:ip is harmless duplication,
        # not a conflict. The gate's /etc/hosts ends up with one line.
        ups = (
            GitGateUpstream(
                name="a", upstream_url="", upstream_host="", upstream_port="",
                identity_file="", known_host_key="",
                extra_hosts={"gitea.dideric.is": "192.0.2.10"},
            ),
            GitGateUpstream(
                name="b", upstream_url="", upstream_host="", upstream_port="",
                identity_file="", known_host_key="",
                extra_hosts={"gitea.dideric.is": "192.0.2.10"},
            ),
        )
        self.assertEqual(
            {"gitea.dideric.is": "192.0.2.10"},
            git_gate_aggregate_extra_hosts(ups),
        )

    def test_aggregator_rejects_conflicting_ips(self):
        ups = (
            GitGateUpstream(
                name="a", upstream_url="", upstream_host="", upstream_port="",
                identity_file="", known_host_key="",
                extra_hosts={"gitea.dideric.is": "192.0.2.10"},
            ),
            GitGateUpstream(
                name="b", upstream_url="", upstream_host="", upstream_port="",
                identity_file="", known_host_key="",
                extra_hosts={"gitea.dideric.is": "10.0.0.99"},
            ),
        )
        with self.assertRaises(Die):
            git_gate_aggregate_extra_hosts(ups)

    def test_aggregator_empty_is_empty(self):
        self.assertEqual({}, git_gate_aggregate_extra_hosts(()))


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
                name="bot-bottle",
                upstream_url="ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
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
            "init_repo 'bot-bottle' "
            "'ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git'",
            script,
        )
        self.assertIn(
            "init_repo 'foo' 'ssh://git@github.com/didericis/foo.git'",
            script,
        )
        # Daemon line is what keeps PID 1 alive.
        self.assertIn("exec git daemon", script)
        self.assertIn("--enable=receive-pack", script)
        self.assertIn("--timeout=60", script)
        self.assertIn("--init-timeout=60", script)
        self.assertIn("--base-path=/git", script)
        # The access-hook is what makes fetch a mirror operation
        # against the upstream (PRD 0008 v1.1).
        self.assertIn("--access-hook=/etc/git-gate/access-hook", script)
        # Each repo's `origin` remote is wired to the upstream via
        # --mirror=fetch so `git fetch origin` mirrors all refs.
        self.assertIn("remote add --mirror=fetch origin", script)

    def test_empty_upstreams_still_execs_daemon(self):
        # A no-upstream gate is a no-op for repos but the daemon still
        # has to start so the entrypoint doesn't exit.
        script = git_gate_render_entrypoint(())
        self.assertNotIn("init_repo '", script)
        self.assertIn("exec git daemon", script)


class TestHookRender(unittest.TestCase):
    def test_pre_receive_hook_has_two_phases(self):
        hook = git_gate_render_hook()
        # Phase 1: gitleaks. Phase 2: forward to origin.
        self.assertIn("gitleaks git", hook)
        self.assertIn("git push origin", hook)
        # KnownHostKey absence is fail-closed.
        self.assertIn("refusing to push", hook)
        # Stdin is buffered to a tempfile so both phases can re-read.
        self.assertIn("refs_file=$(mktemp)", hook)

    def test_new_ref_scan_scoped_to_incoming_commits(self):
        # A new branch (old=all-zeros) must scan only commits new to the
        # gate, not the full ancestry — otherwise historical findings
        # block every new-branch push (PRD 0028 / issue #106).
        hook = git_gate_render_hook()
        self.assertIn('log_opts="$new --not --all"', hook)
        # The old over-broad full-ancestry range must be gone.
        self.assertNotIn('log_opts="$new"', hook)
        # Existing-branch delta scan is unchanged.
        self.assertIn('log_opts="$old..$new"', hook)

    def test_forward_ssh_is_non_interactive_and_bounded(self):
        # No prompt (BatchMode) and a connect timeout, so an unreachable
        # upstream fails fast instead of hanging the receive-pack.
        hook = git_gate_render_hook()
        self.assertIn("BatchMode=yes", hook)
        self.assertIn("ConnectTimeout=", hook)


class TestAccessHookRender(unittest.TestCase):
    def test_access_hook_refreshes_origin_on_upload_pack(self):
        hook = git_gate_render_access_hook()
        # Service-name guard: only upload-pack (fetch / clone / pull /
        # ls-remote) triggers the upstream refresh; receive-pack
        # bypasses this and the pre-receive hook gates it instead.
        self.assertIn('service=$1', hook)
        self.assertIn('"$service" != "upload-pack"', hook)
        # The fetch is what makes the gate a transparent mirror.
        self.assertIn("git -C \"$repo_dir\" fetch origin --prune", hook)

    def test_access_hook_fail_closed_on_upstream_error(self):
        hook = git_gate_render_access_hook()
        # Upstream-fetch failure exits non-zero, which propagates to
        # the agent's fetch as a real error rather than stale data.
        self.assertIn("refusing to serve stale data", hook)
        self.assertIn("exit 1", hook)

    def test_access_hook_ssh_is_non_interactive_and_bounded(self):
        # Same hardening as the forward path: the fetch ssh must not
        # prompt and must time out rather than hang upload-pack.
        hook = git_gate_render_access_hook()
        self.assertIn("BatchMode=yes", hook)
        self.assertIn("ConnectTimeout=", hook)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.stage, ignore_errors=True)

    def test_prepare_writes_all_three_scripts(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        self.assertEqual(
            self.stage / "git_gate_entrypoint.sh", plan.entrypoint_script
        )
        self.assertEqual(
            self.stage / "git_gate_pre_receive.sh", plan.hook_script
        )
        self.assertEqual(
            self.stage / "git_gate_access_hook.sh", plan.access_hook_script
        )
        # Entrypoint + pre-receive are mode 600 (loaded into the
        # gate by docker cp and then `install -m 755`'d into each
        # bare repo's hooks/ — source bit doesn't matter). The
        # access-hook is execed directly by git daemon, so it has to
        # carry the x bit through docker cp.
        self.assertEqual(0o600, os.stat(plan.entrypoint_script).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(plan.hook_script).st_mode & 0o777)
        self.assertEqual(0o700, os.stat(plan.access_hook_script).st_mode & 0o777)

    def test_prepare_plan_carries_upstreams_and_slug(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        self.assertEqual("demo", plan.slug)
        self.assertEqual(2, len(plan.upstreams))
        self.assertEqual("", plan.internal_network)
        self.assertEqual("", plan.egress_network)

    def test_prepare_writes_known_hosts_file(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        upstream = plan.upstreams[0]
        self.assertEqual(self.stage / "bot-bottle-known_hosts",
                         upstream.known_hosts_file)
        self.assertEqual(
            "[gitea.dideric.is]:30009 ssh-ed25519 AAAA...\n",
            upstream.known_hosts_file.read_text(),
        )
        self.assertEqual(0o600, os.stat(upstream.known_hosts_file).st_mode & 0o777)

    def test_prepare_skips_known_hosts_file_when_key_missing(self):
        manifest = Manifest.from_json_obj({
            "bottles": {"dev": {"git": {"remotes": {
                "github.com": {
                    "Name": "foo",
                    "Upstream": "ssh://git@github.com/didericis/foo.git",
                    "IdentityFile": "/dev/null",
                },
            }}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        plan = _StubGate().prepare(
            manifest.bottles["dev"], "demo", self.stage
        )
        self.assertEqual(Path(), plan.upstreams[0].known_hosts_file)

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
