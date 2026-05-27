"""Unit: bundle bringup primitives for the smolmachines backend
(PRD 0023 chunk 2c).

Tests mock `subprocess.run` and assert on the docker argv shape.
The end-to-end integration smoke (real docker daemon, real
bundle image) lands in chunk 2d."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from claude_bottle.backend.smolmachines.sidecar_bundle import (
    BundleLaunchSpec,
    bundle_container_name,
    bundle_network_name,
    create_bundle_network,
    remove_bundle_network,
    start_bundle,
    stop_bundle,
)


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr,
    )


def _spec(**kwargs) -> BundleLaunchSpec:
    defaults = dict(
        slug="demo-abc12",
        network_name="claude-bottle-bundle-demo-abc12",
        subnet="192.168.50.0/24",
        gateway="192.168.50.1",
        bundle_ip="192.168.50.2",
    )
    defaults.update(kwargs)
    return BundleLaunchSpec(**defaults)


class TestNamingHelpers(unittest.TestCase):
    def test_network_name_uses_bundle_prefix(self):
        # Distinct from the docker backend's
        # `claude-bottle-net-<slug>` so two backends running the
        # same agent slug don't collide.
        self.assertEqual(
            "claude-bottle-bundle-myagent-xyz",
            bundle_network_name("myagent-xyz"),
        )

    def test_container_name_matches_docker_bundle_shape(self):
        # Same shape PRD 0024 chunk 5 set for the docker backend's
        # bundle container — dashboard prefix-discovery covers
        # both backends with one filter.
        self.assertEqual(
            "claude-bottle-sidecars-myagent-xyz",
            bundle_container_name("myagent-xyz"),
        )


class TestNetworkLifecycle(unittest.TestCase):
    def _patch_run(self, **kwargs):
        return patch(
            "claude_bottle.backend.smolmachines.sidecar_bundle.subprocess.run",
            **kwargs,
        )

    def test_create_argv_explicit_subnet_and_gateway(self):
        with self._patch_run(return_value=_ok()) as m:
            create_bundle_network("nn", "192.168.50.0/24", "192.168.50.1")
        self.assertEqual(
            ["docker", "network", "create",
             "--subnet", "192.168.50.0/24",
             "--gateway", "192.168.50.1",
             "nn"],
            m.call_args.args[0],
        )

    def test_create_treats_existing_network_as_success(self):
        with self._patch_run(return_value=_fail("network nn already exists")):
            # No SystemExit.
            create_bundle_network("nn", "192.168.50.0/24", "192.168.50.1")

    def test_create_other_failure_is_fatal(self):
        with self._patch_run(return_value=_fail("invalid subnet")):
            with self.assertRaises(SystemExit):
                create_bundle_network("nn", "bogus", "bogus")

    def test_remove_missing_network_is_idempotent(self):
        # No SystemExit / no warn-and-continue noise; missing
        # network is the expected case during a partial teardown.
        with self._patch_run(return_value=_fail("Error: No such network: nn")):
            remove_bundle_network("nn")

    def test_remove_clean_returns_success(self):
        with self._patch_run(return_value=_ok()):
            remove_bundle_network("nn")


class TestStartBundle(unittest.TestCase):
    def _patch_run(self):
        return patch(
            "claude_bottle.backend.smolmachines.sidecar_bundle.subprocess.run",
            return_value=_ok(),
        )

    def test_argv_pins_ip_on_network(self):
        with self._patch_run() as m:
            start_bundle(_spec())
        argv = m.call_args.args[0]
        # --network NETNAME --ip <bundle-ip> on the docker run.
        self.assertIn("--network", argv)
        self.assertIn("claude-bottle-bundle-demo-abc12", argv)
        self.assertIn("--ip", argv)
        self.assertIn("192.168.50.2", argv)
        # Detached and auto-removed.
        self.assertIn("--detach", argv)
        self.assertIn("--rm", argv)
        # Container name uses the per-slug bundle prefix.
        i = argv.index("--name")
        self.assertEqual("claude-bottle-sidecars-demo-abc12", argv[i + 1])
        # Image at the end.
        self.assertEqual("claude-bottle-sidecars:latest", argv[-1])

    def test_daemons_env_passed_in(self):
        with self._patch_run() as m:
            start_bundle(_spec(daemons_csv="egress,pipelock,supervise"))
        argv = m.call_args.args[0]
        self.assertIn("-e", argv)
        self.assertIn(
            "CLAUDE_BOTTLE_SIDECAR_DAEMONS=egress,pipelock,supervise",
            argv,
        )

    def test_environment_entries_pass_through(self):
        with self._patch_run() as m:
            start_bundle(_spec(environment=(
                "EGRESS_UPSTREAM_PROXY=http://...",
                "SUPERVISE_BOTTLE_SLUG=demo-abc12",
                "EGRESS_TOKEN_0",  # bare-name → host env inherit
            )))
        argv = m.call_args.args[0]
        self.assertIn("EGRESS_UPSTREAM_PROXY=http://...", argv)
        self.assertIn("SUPERVISE_BOTTLE_SLUG=demo-abc12", argv)
        self.assertIn("EGRESS_TOKEN_0", argv)

    def test_volumes_render_with_ro_flag(self):
        with self._patch_run() as m:
            start_bundle(_spec(volumes=(
                ("/host/pipelock.yaml", "/etc/pipelock.yaml", True),
                ("/host/queue", "/run/supervise/queue", False),
            )))
        argv = m.call_args.args[0]
        self.assertIn("/host/pipelock.yaml:/etc/pipelock.yaml:ro", argv)
        self.assertIn("/host/queue:/run/supervise/queue", argv)

    def test_failure_dies(self):
        with patch(
            "claude_bottle.backend.smolmachines.sidecar_bundle.subprocess.run",
            return_value=_fail("invalid mount"),
        ):
            with self.assertRaises(SystemExit):
                start_bundle(_spec())

    def test_host_env_inherited_to_subprocess(self):
        # Bare-name entries in spec.environment rely on the docker
        # subprocess being run with the host env. Confirm `env=`
        # threads through.
        with patch(
            "claude_bottle.backend.smolmachines.sidecar_bundle.subprocess.run",
            return_value=_ok(),
        ) as m:
            start_bundle(_spec(), env={"FOO": "bar"})
        self.assertEqual({"FOO": "bar"}, m.call_args.kwargs["env"])


class TestStopBundle(unittest.TestCase):
    def _patch_run(self, **kwargs):
        return patch(
            "claude_bottle.backend.smolmachines.sidecar_bundle.subprocess.run",
            **kwargs,
        )

    def test_argv_force_removes(self):
        with self._patch_run(return_value=_ok()) as m:
            stop_bundle("demo-abc12")
        self.assertEqual(
            ["docker", "rm", "-f", "claude-bottle-sidecars-demo-abc12"],
            m.call_args.args[0],
        )

    def test_missing_container_is_idempotent(self):
        with self._patch_run(return_value=_fail(
            "Error: No such container: claude-bottle-sidecars-demo-abc12"
        )):
            stop_bundle("demo-abc12")  # no raise


if __name__ == "__main__":
    unittest.main()
