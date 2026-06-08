"""Integration: drive `apply_capability_change` against a real
container that mimics the agent's name + filesystem layout (PRD 0016).

The real `cli.py start <agent>` flow is too heavy for an integration
test (it builds the agent image, brings up all the sidecars, attaches
an interactive agent session). Instead, this test stages the
minimum the orchestrator interacts with:

  - A lightweight `alpine:latest sleep infinity` container named
    `bot-bottle-<slug>` (matches the agent container name pattern)
    on the per-bottle internal network.
  - A marker file under `/home/node/.claude/` so we can assert the
    transcript snapshot path actually transferred bytes.

Then `apply_capability_change` runs and we verify:
  - Per-bottle Dockerfile written.
  - Containers + networks removed.
  - Transcript snapshot dir on the host has the marker file.

docker exec / cp / rm work across the docker socket boundary, so
this test runs in DinD too — no act_runner skip needed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from bot_bottle import supervise
from bot_bottle import bottle_state
from bot_bottle.backend.docker.capability_apply import apply_capability_change
from bot_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from bot_bottle.backend.docker.sidecar_bundle import (
    sidecar_bundle_container_name,
)
from tests._docker import skip_unless_docker


ALPINE_IMAGE = "alpine:latest"


@skip_unless_docker()
class TestCapabilityApply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(
            ["docker", "pull", ALPINE_IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if r.returncode != 0:
            raise unittest.SkipTest(f"could not pull {ALPINE_IMAGE}")

    def setUp(self):
        self.slug = f"cb-test-cap-{os.getpid()}-{int(time.time())}"
        self.agent_name = f"bot-bottle-{self.slug}"
        self.sidecar_names: list[str] = []
        self.internal_net = ""
        self.egress_net = ""
        # Fake home so tests don't touch ~/.bot-bottle/.
        self._tmp = tempfile.TemporaryDirectory(prefix="cap-apply-int.")
        self._original_root = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]

    def tearDown(self):
        supervise.bot_bottle_root = self._original_root  # type: ignore[assignment]
        for name in [self.agent_name, *self.sidecar_names]:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        self._tmp.cleanup()

    def _bring_up_fake_bottle(self) -> None:
        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)
        # Agent container with the canonical name.
        r = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", self.agent_name,
                "--network", self.internal_net,
                ALPINE_IMAGE,
                "sh", "-c",
                "mkdir -p /home/node/.claude && "
                "echo 'transcript-marker' > /home/node/.claude/sessions.json && "
                "sleep 3600",
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, r.returncode, r.stderr)
        # Also start a fake sidecar bundle so teardown has something
        # extra to clean up (mirrors a real bottle's container set).
        sidecar = sidecar_bundle_container_name(self.slug)
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", sidecar,
                "--network", self.internal_net,
                ALPINE_IMAGE, "sleep", "3600",
            ],
            capture_output=True, text=True, check=False,
        )
        self.sidecar_names.append(sidecar)

    def _containers_named_like(self) -> list[str]:
        """All running/stopped containers whose names start with
        the bottle's slug — both agent + sidecars."""
        r = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"name={self.agent_name}",
                "--format", "{{.Names}}",
            ],
            capture_output=True, text=True, check=False,
        )
        return [line for line in (r.stdout or "").splitlines() if line]

    def _networks_named_like(self) -> list[str]:
        r = subprocess.run(
            [
                "docker", "network", "ls",
                "--filter", f"name={self.slug}",
                "--format", "{{.Name}}",
            ],
            capture_output=True, text=True, check=False,
        )
        return [line for line in (r.stdout or "").splitlines() if line]

    def test_apply_writes_dockerfile_and_tears_down(self):
        self._bring_up_fake_bottle()
        self.assertIn(self.agent_name, self._containers_named_like())

        new_dockerfile = "FROM python:3.13\nRUN apk add ripgrep\n"
        before, after = apply_capability_change(self.slug, new_dockerfile)

        # Before is the repo Dockerfile (no prior per-bottle override);
        # after is what we passed in.
        self.assertIn("FROM ", before)
        self.assertEqual(new_dockerfile, after)

        # Per-bottle Dockerfile written on the host.
        self.assertEqual(
            new_dockerfile,
            bottle_state.per_bottle_dockerfile(self.slug),
        )

        # Agent + sidecars gone.
        self.assertEqual([], self._containers_named_like())
        # Networks removed (matching the slug substring).
        nets = self._networks_named_like()
        self.assertEqual([], nets)
        # Mark them as already cleaned so tearDown is idempotent.
        self.internal_net = ""
        self.egress_net = ""
        self.sidecar_names = []

    def test_transcript_snapshot_captured(self):
        self._bring_up_fake_bottle()
        apply_capability_change(self.slug, "FROM x\n")
        snap = bottle_state.transcript_snapshot_dir(self.slug)
        self.assertTrue(snap.is_dir(), f"transcript snapshot dir {snap} missing")
        # docker cp <container>:/home/node/.claude <dst>  produces
        # <dst>/.claude/sessions.json (it preserves the source dir name
        # inside the destination if the destination already exists).
        # Walk the snapshot looking for the marker contents.
        marker_found = False
        for path in snap.rglob("sessions.json"):
            if "transcript-marker" in path.read_text():
                marker_found = True
                break
        self.assertTrue(marker_found, f"marker not found under {snap}")
        # Cleaned up by apply already.
        self.internal_net = ""
        self.egress_net = ""
        self.sidecar_names = []

    def test_subsequent_apply_uses_per_bottle_dockerfile_for_before(self):
        # First change: before is repo's Dockerfile.
        self._bring_up_fake_bottle()
        first_before, _ = apply_capability_change(self.slug, "FROM v1\n")
        self.assertIn("FROM ", first_before)

        # Second change: before is "FROM v1\n" (the per-bottle override
        # from the first change), proving the state persists across
        # rebuilds.
        self._bring_up_fake_bottle()
        second_before, second_after = apply_capability_change(self.slug, "FROM v2\n")
        self.assertEqual("FROM v1\n", second_before)
        self.assertEqual("FROM v2\n", second_after)
        self.internal_net = ""
        self.egress_net = ""
        self.sidecar_names = []

    def test_teardown_idempotent_when_nothing_running(self):
        # No bottle ever brought up — teardown still doesn't raise.
        apply_capability_change(self.slug, "FROM x\n")
        self.assertEqual(
            "FROM x\n",
            bottle_state.per_bottle_dockerfile(self.slug),
        )


if __name__ == "__main__":
    unittest.main()
