"""Integration: drive `apply_allowlist_change` against a real
pipelock sidecar (PRD 0015).

Brings up a real pipelock container via direct `docker run` (the
old `.start()` helper went away in PRD 0024 chunk 3), calls
apply_allowlist_change to swap the api_allowlist, restarts
pipelock, and verifies the running container now serves the new
yaml.

The hot-reload code path under test (apply_allowlist_change,
fetch_current_yaml, fetch_current_allowlist) is unchanged from
PRD 0015 — only the test's bringup helper moved.

Setup uses pipelock_tls_init which bind-mounts a host path into a
one-shot pipelock container — that doesn't work in DinD, so the
test skips under GITEA_ACTIONS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from claude_bottle.backend.docker.bottle_state import pipelock_state_dir
from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from claude_bottle.backend.docker.pipelock import (
    PIPELOCK_CA_CERT_IN_CONTAINER,
    PIPELOCK_CA_KEY_IN_CONTAINER,
    PIPELOCK_IMAGE,
    PIPELOCK_PORT,
    DockerPipelockProxy,
    pipelock_tls_init,
)
from claude_bottle.backend.docker.pipelock_apply import (
    PipelockApplyError,
    apply_allowlist_change,
    fetch_current_allowlist,
    fetch_current_yaml,
)
from claude_bottle.backend.docker.sidecar_bundle import (
    sidecar_bundle_container_name,
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
            subprocess.run(
                ["docker", "rm", "-f", self.sidecar_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        # Clean up the per-slug state dir under ~/.claude-bottle/state/
        # (apply_allowlist_change writes there; _bring_up calls
        # proxy.prepare with the same path so the bind-mount and the
        # hot-reload write target stay coherent).
        shutil.rmtree(pipelock_state_dir(self.slug), ignore_errors=True)

    def _bring_up(self) -> None:
        """Replicates the pre-chunk-3 bring-up sequence (create on
        internal network → bind-mount yaml + CAs → attach egress
        network → docker start) without going through the deleted
        `DockerPipelockProxy.start` helper. The same sequence is
        what `docker compose up` does for the pipelock service in
        production; this test path keeps the standalone-pipelock
        smoke alive so `apply_allowlist_change`'s host-side
        write + docker-restart loop has integration coverage.

        The yaml stages into the production-real
        `pipelock_state_dir(slug)` (not a private temp dir) so the
        bind-mount target matches what `apply_allowlist_change`
        writes to — otherwise the hot-reload would write to a
        nowhere-mounted host path and the container would never see
        the updated config."""
        state_dir = pipelock_state_dir(self.slug)
        state_dir.mkdir(parents=True, exist_ok=True)
        prep = DockerPipelockProxy().prepare(
            fixture_minimal().bottles["dev"], self.slug, state_dir,
        )
        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)
        ca_cert_host, ca_key_host = pipelock_tls_init(state_dir)

        # apply_allowlist_change targets sidecar_bundle_container_name
        # (chunk 5 flipped the bundle to the only shape). Bringing the
        # standalone pipelock up under that name keeps this test
        # exercising the real production code path; the bundle's
        # other three daemons aren't running here, but the
        # apply/fetch code only touches /etc/pipelock.yaml + the
        # pipelock binary, so the lighter setup is fine.
        self.sidecar_name = sidecar_bundle_container_name(self.slug)
        subprocess.run(
            ["docker", "create",
             "--name", self.sidecar_name,
             "--network", self.internal_net,
             "-v", f"{prep.yaml_path}:/etc/pipelock.yaml:ro",
             "-v", f"{ca_cert_host}:{PIPELOCK_CA_CERT_IN_CONTAINER}:ro",
             "-v", f"{ca_key_host}:{PIPELOCK_CA_KEY_IN_CONTAINER}:ro",
             PIPELOCK_IMAGE,
             "run", "--config", "/etc/pipelock.yaml",
             "--listen", f"0.0.0.0:{PIPELOCK_PORT}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["docker", "network", "connect", self.egress_net, self.sidecar_name],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["docker", "start", self.sidecar_name],
            check=True, capture_output=True,
        )
        # Wait until fetch_current_yaml succeeds — it's a docker cp
        # which works on a started-but-not-yet-ready pipelock, so
        # this is more of a "container exists" probe than a
        # readiness one; the hot-reload tests below tolerate
        # pipelock briefly being slow to serve.
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
