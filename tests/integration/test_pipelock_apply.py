"""Integration: drive `apply_allowlist_change` against a real
pipelock sidecar (PRD 0015).

Brings up a real pipelock sidecar (via the production DockerPipelockProxy
bring-up), calls apply_allowlist_change to swap the api_allowlist,
restarts pipelock, and verifies the running container now serves the
new yaml.

Setup uses pipelock_tls_init which bind-mounts a host path into a
one-shot pipelock container — that doesn't work in DinD, so the test
skips under GITEA_ACTIONS the same way the existing pipelock smoke
test does.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from claude_bottle.backend.docker.pipelock import (
    DockerPipelockProxy,
    pipelock_container_name,
    pipelock_tls_init,
)
from claude_bottle.backend.docker.pipelock_apply import (
    PipelockApplyError,
    apply_allowlist_change,
    fetch_current_allowlist,
    fetch_current_yaml,
)
from claude_bottle.yaml_subset import parse_yaml_subset
from tests._docker import skip_unless_docker
from tests.fixtures import fixture_minimal


@skip_unless_docker()
@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: pipelock_tls_init uses a host bind mount "
    "that doesn't share fs with the runner container",
)
class TestPipelockApply(unittest.TestCase):
    def setUp(self):
        self.slug = f"cb-test-pla-{os.getpid()}-{int(time.time())}"
        self.sidecar_name = ""
        self.internal_net = ""
        self.egress_net = ""
        self.work_dir = Path(tempfile.mkdtemp(prefix="pipelock-apply."))

    def tearDown(self):
        if self.sidecar_name:
            DockerPipelockProxy().stop(self.sidecar_name)
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _bring_up(self) -> None:
        proxy = DockerPipelockProxy()
        prep = proxy.prepare(fixture_minimal().bottles["dev"], self.slug, self.work_dir)
        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)
        ca_cert_host, ca_key_host = pipelock_tls_init(self.work_dir)
        plan = dataclasses.replace(
            prep,
            internal_network=self.internal_net,
            egress_network=self.egress_net,
            ca_cert_host_path=ca_cert_host,
            ca_key_host_path=ca_key_host,
        )
        self.sidecar_name = proxy.start(plan)
        self.assertEqual(pipelock_container_name(self.slug), self.sidecar_name)
        # Wait until docker exec succeeds — the container is up but
        # pipelock may still be initializing. fetch_current_yaml is
        # itself a docker exec, so retrying it doubles as a readiness
        # probe.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                fetch_current_yaml(self.slug)
                return
            except PipelockApplyError:
                pass
            time.sleep(0.25)
        raise AssertionError("pipelock sidecar never became reachable")

    def _wait_for_yaml(self, contains: str, *, deadline_s: float = 15.0) -> str:
        """Poll docker exec until /etc/pipelock.yaml contains `contains`,
        returning the yaml. Used to bridge the docker-restart window."""
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                yaml = fetch_current_yaml(self.slug)
                if contains in yaml:
                    return yaml
            except PipelockApplyError:
                pass
            time.sleep(0.25)
        self.fail(f"never saw {contains!r} in /etc/pipelock.yaml")

    def test_apply_swaps_api_allowlist(self):
        self._bring_up()

        initial_yaml = fetch_current_yaml(self.slug)
        # fixture_minimal yields the baked-in DEFAULT_ALLOWLIST in
        # pipelock.py; api.anthropic.com is in there.
        self.assertIn("api.anthropic.com", initial_yaml)

        new_content = "api.anthropic.com\nnew-host.example\n"
        before, after = apply_allowlist_change(self.slug, new_content)
        self.assertIn("api.anthropic.com", before)
        self.assertNotIn("new-host.example", before)
        self.assertIn("new-host.example", after)

        updated = self._wait_for_yaml("new-host.example")
        cfg = parse_yaml_subset(updated)
        self.assertIn("new-host.example", cfg["api_allowlist"])  # type: ignore[operator]
        self.assertIn("api.anthropic.com", cfg["api_allowlist"])  # type: ignore[operator]
        # tls_interception block (set up by the production prepare
        # via pipelock_build_config) is preserved across the swap.
        self.assertIn("tls_interception", cfg)

    def test_apply_with_invalid_host_raises(self):
        self._bring_up()
        with self.assertRaises(PipelockApplyError):
            apply_allowlist_change(self.slug, "host with space.example\n")

    def test_fetch_current_allowlist_renders_one_per_line(self):
        self._bring_up()
        listing = fetch_current_allowlist(self.slug)
        self.assertTrue(listing.endswith("\n"))
        self.assertIn("api.anthropic.com\n", listing)

    def test_apply_against_missing_sidecar_raises(self):
        # Don't bring up — the slug points at nothing.
        with self.assertRaises(PipelockApplyError):
            apply_allowlist_change(self.slug, "x.example\n")


if __name__ == "__main__":
    unittest.main()
