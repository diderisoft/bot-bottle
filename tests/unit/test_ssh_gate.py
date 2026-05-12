"""Unit: SSHGate prepare shape + entrypoint render."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from claude_bottle.manifest import Manifest
from claude_bottle.ssh_gate import (
    SSHGate,
    SSHGatePlan,
    SSHGateUpstream,
    ssh_gate_render_entrypoint,
    ssh_gate_upstreams_for_bottle,
)
from tests.fixtures import fixture_minimal, fixture_with_ssh


class _StubGate(SSHGate):
    """Concrete subclass for testing the abstract `prepare`. The
    backend-specific start/stop aren't exercised here."""

    def start(self, plan: SSHGatePlan) -> str:
        raise NotImplementedError

    def stop(self, target: str) -> None:
        raise NotImplementedError


class TestUpstreamAssignment(unittest.TestCase):
    def test_listen_port_matches_upstream_port(self):
        # Critical: URLs like ssh://git@host:30009/... override the
        # config Port directive, so the gate must listen on the same
        # port the URL names.
        bottle = fixture_with_ssh().bottles["dev"]
        upstreams = ssh_gate_upstreams_for_bottle(bottle)
        self.assertEqual(2, len(upstreams))
        # Fixture: tailscale-gitea -> 192.0.2.10:30009, github -> github.com:22.
        self.assertEqual(30009, upstreams[0].listen_port)
        self.assertEqual(22, upstreams[1].listen_port)

    def test_upstream_fields_mirror_ssh_entry(self):
        bottle = fixture_with_ssh().bottles["dev"]
        first = ssh_gate_upstreams_for_bottle(bottle)[0]
        self.assertEqual("tailscale-gitea", first.bottle_host_alias)
        self.assertEqual("192.0.2.10", first.upstream_host)
        self.assertEqual("30009", first.upstream_port)

    def test_empty_bottle_yields_empty_upstreams(self):
        bottle = fixture_minimal().bottles["dev"]
        self.assertEqual((), ssh_gate_upstreams_for_bottle(bottle))

    def test_duplicate_upstream_port_is_rejected(self):
        # Two entries on the same upstream port can't both have a
        # listener — the gate is one container with a flat port
        # space. Surface as a clear config error.
        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "ssh": [
                        {"Host": "a", "IdentityFile": "/dev/null",
                         "Hostname": "host-a.example", "User": "git", "Port": 22},
                        {"Host": "b", "IdentityFile": "/dev/null",
                         "Hostname": "host-b.example", "User": "git", "Port": 22},
                    ],
                }
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        with self.assertRaises(SystemExit):
            ssh_gate_upstreams_for_bottle(manifest.bottles["dev"])


class TestEntrypointRender(unittest.TestCase):
    def test_one_socat_line_per_upstream(self):
        upstreams = (
            SSHGateUpstream(30009, "gitea.example", "30009", "gitea"),
            SSHGateUpstream(22, "github.com", "22", "gh"),
        )
        script = ssh_gate_render_entrypoint(upstreams)
        self.assertIn("#!/bin/sh", script)
        self.assertIn(
            "socat TCP-LISTEN:30009,reuseaddr,fork TCP:gitea.example:30009 &", script
        )
        self.assertIn(
            "socat TCP-LISTEN:22,reuseaddr,fork TCP:github.com:22 &", script
        )
        # wait blocks the entrypoint so PID 1 stays alive while sockets
        # are open.
        self.assertTrue(script.rstrip().endswith("wait"))

    def test_empty_upstreams_still_has_wait(self):
        # Defensive: a no-upstream gate is a no-op, but render must
        # still produce a valid shell script.
        script = ssh_gate_render_entrypoint(())
        self.assertIn("#!/bin/sh", script)
        self.assertIn("wait", script)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.stage, ignore_errors=True)

    def test_prepare_writes_entrypoint_mode_600(self):
        plan = _StubGate().prepare(
            fixture_with_ssh().bottles["dev"], "demo", self.stage
        )
        self.assertEqual(self.stage / "ssh_gate_entrypoint.sh", plan.entrypoint_script)
        self.assertEqual(0o600, os.stat(plan.entrypoint_script).st_mode & 0o777)

    def test_prepare_plan_carries_upstreams_and_slug(self):
        plan = _StubGate().prepare(
            fixture_with_ssh().bottles["dev"], "demo", self.stage
        )
        self.assertEqual("demo", plan.slug)
        self.assertEqual(2, len(plan.upstreams))
        self.assertEqual("", plan.internal_network)
        self.assertEqual("", plan.egress_network)

    def test_prepare_with_no_ssh_writes_minimal_script(self):
        plan = _StubGate().prepare(
            fixture_minimal().bottles["dev"], "demo", self.stage
        )
        self.assertEqual((), plan.upstreams)
        content = plan.entrypoint_script.read_text()
        self.assertNotIn("socat", content)
        self.assertIn("wait", content)


if __name__ == "__main__":
    unittest.main()
