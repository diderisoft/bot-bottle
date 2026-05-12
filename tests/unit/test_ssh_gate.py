"""Unit: SSHGate prepare shape + entrypoint render."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from claude_bottle.ssh_gate import (
    BASE_LISTEN_PORT,
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
    def test_indexed_listen_ports(self):
        bottle = fixture_with_ssh().bottles["dev"]
        upstreams = ssh_gate_upstreams_for_bottle(bottle)
        self.assertEqual(2, len(upstreams))
        self.assertEqual(BASE_LISTEN_PORT, upstreams[0].listen_port)
        self.assertEqual(BASE_LISTEN_PORT + 1, upstreams[1].listen_port)

    def test_upstream_fields_mirror_ssh_entry(self):
        bottle = fixture_with_ssh().bottles["dev"]
        first = ssh_gate_upstreams_for_bottle(bottle)[0]
        # The fixture's first ssh entry: tailscale-gitea / 192.0.2.10:30009.
        self.assertEqual("tailscale-gitea", first.bottle_host_alias)
        self.assertEqual("192.0.2.10", first.upstream_host)
        self.assertEqual("30009", first.upstream_port)

    def test_empty_bottle_yields_empty_upstreams(self):
        bottle = fixture_minimal().bottles["dev"]
        self.assertEqual((), ssh_gate_upstreams_for_bottle(bottle))


class TestEntrypointRender(unittest.TestCase):
    def test_one_socat_line_per_upstream(self):
        upstreams = (
            SSHGateUpstream(30000, "gitea.example", "22", "gitea"),
            SSHGateUpstream(30001, "github.com", "22", "gh"),
        )
        script = ssh_gate_render_entrypoint(upstreams)
        self.assertIn("#!/bin/sh", script)
        self.assertIn(
            "socat TCP-LISTEN:30000,reuseaddr,fork TCP:gitea.example:22 &", script
        )
        self.assertIn(
            "socat TCP-LISTEN:30001,reuseaddr,fork TCP:github.com:22 &", script
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
