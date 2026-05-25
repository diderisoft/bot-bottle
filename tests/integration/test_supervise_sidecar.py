"""Integration: drive `DockerSupervise.start` against the supervise
sidecar and round-trip an MCP tool call through the queue (PRD 0013).

Topology mirrors production minimally: a per-bottle internal docker
network for the agent ↔ supervise leg, no egress network (supervise
doesn't make outbound calls). The "agent" is a curl container on the
internal net; the supervisor lives on the host (this test process)
and uses claude_bottle.cli.dashboard helpers to write Response files.

Verifies:
  1. `tools/list` returns the three PRD 0013 tool names over real MCP
     wire format.
  2. A `tools/call` from the in-container agent blocks until the host
     writes a Response to the queue; once written, the agent receives
     the approval payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claude_bottle import supervise as _sv
from claude_bottle.backend.docker.network import (
    network_create_internal,
    network_remove,
)
from claude_bottle.backend.docker.supervise import (
    DockerSupervise,
    build_supervise_image,
    supervise_container_name,
)
from claude_bottle.cli import dashboard
from claude_bottle.supervise import SupervisePlan, list_pending_proposals
from tests._docker import skip_unless_docker


CURL_IMAGE = "curlimages/curl:latest"


@skip_unless_docker()
class TestSuperviseSidecar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(
            ["docker", "pull", CURL_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if r.returncode != 0:
            raise unittest.SkipTest(f"could not pull {CURL_IMAGE}")
        build_supervise_image()

    def setUp(self):
        self.slug = f"cb-test-sv-{os.getpid()}-{int(time.time())}"
        self.sidecar_name = ""
        self.internal_net = ""
        self.work_dir = Path(tempfile.mkdtemp(prefix="supervise-int."))
        self.queue_dir = self.work_dir / "queue"
        self.queue_dir.mkdir()

    def tearDown(self):
        if self.sidecar_name:
            subprocess.run(
                ["docker", "rm", "-f", self.sidecar_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if self.internal_net:
            network_remove(self.internal_net)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _require_bind_mount_sharing(self) -> None:
        """Skip if `docker run -v <host-path>:<container-path>` doesn't
        share the filesystem between the test process and the spawned
        container. In docker-in-docker CI (Gitea Actions runner with
        host socket forwarded), bind-mount paths are resolved against
        the outer host's fs, not the runner container's — so the
        sidecar writes proposals to a dir the test process can't see.

        Cached on the class so the probe runs once per test session."""
        cached = getattr(type(self), "_bind_mount_ok", None)
        if cached is True:
            return
        if cached is False:
            self.skipTest(
                "docker bind mounts don't share fs with this test process "
                "(likely docker-in-docker); the supervise queue round-trip "
                "requires real host fs sharing"
            )
        probe_dir = Path(tempfile.mkdtemp(prefix="supervise-bind-probe."))
        try:
            (probe_dir / "from-host").write_text("x")
            r = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{probe_dir}:/probe",
                    "--entrypoint", "sh",
                    CURL_IMAGE,
                    "-c", "test -f /probe/from-host && touch /probe/from-container",
                ],
                capture_output=True,
                check=False,
            )
            ok = (
                r.returncode == 0
                and (probe_dir / "from-container").exists()
            )
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
        type(self)._bind_mount_ok = ok
        if not ok:
            self.skipTest(
                "docker bind mounts don't share fs with this test process "
                "(likely docker-in-docker); the supervise queue round-trip "
                "requires real host fs sharing"
            )

    def _bring_up_sidecar(self) -> None:
        self.internal_net = network_create_internal(self.slug)
        plan = SupervisePlan(
            slug=self.slug,
            queue_dir=self.queue_dir,
            current_config_dir=self.work_dir / "current-config",
            internal_network=self.internal_net,
        )
        # current_config_dir isn't bind-mounted into the sidecar, only
        # the queue dir is. Create it for symmetry with production.
        plan.current_config_dir.mkdir()
        self.sidecar_name = DockerSupervise().start(plan)

        # Block until the server is ready to answer (the container
        # `docker start` returns immediately; python is still
        # binding to the port).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            rc = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--network", self.internal_net,
                    CURL_IMAGE,
                    "-fsS", "-o", "/dev/null",
                    "--max-time", "2",
                    f"http://{_sv.SUPERVISE_HOSTNAME}:{_sv.SUPERVISE_PORT}/health",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            if rc == 0:
                return
            time.sleep(0.25)
        raise AssertionError("supervise sidecar /health never came up")

    def _curl_jsonrpc(self, body: dict[str, object]) -> dict[str, object]:
        """Invoke curl on the internal network to POST a JSON-RPC
        request to the supervise sidecar and parse the response."""
        payload = json.dumps(body)
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", self.internal_net,
                CURL_IMAGE,
                "-sS", "--max-time", "30",
                "-H", "Content-Type: application/json",
                "-X", "POST",
                "--data", payload,
                f"http://{_sv.SUPERVISE_HOSTNAME}:{_sv.SUPERVISE_PORT}/",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"curl to supervise failed: {result.stderr}\n"
                f"stdout: {result.stdout}"
            )
        return json.loads(result.stdout)

    def test_tools_list_over_mcp(self):
        self._bring_up_sidecar()
        result = self._curl_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(1, result["id"])
        names = {t["name"] for t in result["result"]["tools"]}
        self.assertEqual(
            {
                _sv.TOOL_CRED_PROXY_BLOCK,
                _sv.TOOL_PIPELOCK_BLOCK,
                _sv.TOOL_CAPABILITY_BLOCK,
            },
            names,
        )

    def test_tools_call_round_trips_through_queue(self):
        """End-to-end: agent in the bottle calls cred-proxy-block;
        the call blocks on the queue; the host rejects via the
        dashboard helpers; the agent receives the rejection.

        PRD 0017 chunk 2 deleted the cred-proxy sidecar, so the
        approval-apply path on cred-proxy-block is broken in this
        intermediate state (chunk 3 retargets it at egress-proxy and
        restores the round-trip approval test). For now this verifies
        only the queue + response leg by exercising the reject path
        — no docker-exec into a sidecar needed."""
        self._require_bind_mount_sharing()
        self._bring_up_sidecar()

        captured: dict[str, object] = {}

        def caller() -> None:
            captured["response"] = self._curl_jsonrpc({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {
                    "name": _sv.TOOL_CRED_PROXY_BLOCK,
                    "arguments": {
                        "routes": '{"routes": [{"path": "/x/"}]}',
                        "justification": "integration test",
                    },
                },
            })

        t = threading.Thread(target=caller)
        t.start()
        try:
            # Wait for the proposal to appear in the queue (the
            # sidecar writes it before blocking on wait_for_response).
            deadline = time.monotonic() + 10.0
            qp = None
            while time.monotonic() < deadline:
                pending = list_pending_proposals(self.queue_dir)
                if pending:
                    qp = dashboard.QueuedProposal(
                        proposal=pending[0], queue_dir=self.queue_dir,
                    )
                    break
                time.sleep(0.1)
            self.assertIsNotNone(qp, "proposal never appeared in queue")
            assert qp is not None  # type-narrowing
            self.assertEqual(
                _sv.TOOL_CRED_PROXY_BLOCK, qp.proposal.tool,
            )
            self.assertEqual("integration test", qp.proposal.justification)

            # Reject via the dashboard helper. The reject path skips
            # the sidecar-apply step, so it works without a real
            # cred-proxy sidecar (which doesn't exist in chunk 2's
            # transitional state).
            dashboard.reject(qp, reason="no real cred-proxy in chunk 2")
        finally:
            t.join(timeout=20)

        response = captured.get("response")
        self.assertIsNotNone(response, "curl thread never produced a response")
        assert isinstance(response, dict)  # type-narrowing
        self.assertEqual(7, response["id"])
        result = response["result"]
        assert isinstance(result, dict)
        # Rejected tool calls surface as MCP errors so the agent
        # treats them as failures (not silent successes).
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertIn("rejected", text)
        self.assertIn("no real cred-proxy", text)

    def test_orphan_sidecar_name_collision_recovered(self):
        """An orphan supervise sidecar from a previous run blocks
        the next .start with a duplicate-name error. Documents the
        observed behavior so a future change that adds auto-cleanup
        can flip the assertion."""
        self._bring_up_sidecar()
        self.assertEqual(supervise_container_name(self.slug), self.sidecar_name)
        # Second .start should fail because the container name is
        # taken. cleanup is handled by the orphan probe in prepare.py
        # (tested separately in test_orphan_cleanup).
        with self.assertRaises(SystemExit):
            DockerSupervise().start(SupervisePlan(
                slug=self.slug,
                queue_dir=self.queue_dir,
                current_config_dir=self.work_dir / "current-config",
                internal_network=self.internal_net,
            ))


if __name__ == "__main__":
    unittest.main()
