"""Unit: the docker orchestrator (control plane) container lifecycle (PRD 0070)."""

from __future__ import annotations

import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.docker.orchestrator import (
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_NETWORK,
    ORCHESTRATOR_SOURCE_HASH_LABEL,
    DockerOrchestrator,
)
from bot_bottle.gateway import GatewayError
from bot_bottle.orchestrator.lifecycle import OrchestratorStartError, source_hash

_ORCH = "bot_bottle.backend.docker.orchestrator"
_RUN = f"{_ORCH}.run_docker"
_SLEEP = f"{_ORCH}.time.sleep"
_MONOTONIC = f"{_ORCH}.time.monotonic"
# The signing key is read through the shared provisioning contract (#476); patch
# its host-canonical key file read to keep it off the real host file.
_TOKEN = "bot_bottle.trust_domain.host_signing_key"
# The ABC's is_healthy probes /health via urllib in the lifecycle module.
_URLOPEN = "bot_bottle.orchestrator.lifecycle.urllib.request.urlopen"


def _health(status: int) -> MagicMock:
    m = MagicMock()
    m.__enter__.return_value.status = status
    return m


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestDockerOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = DockerOrchestrator(port=8099)
        # _run_container seeds the signing key; keep it off the real host file.
        p = patch(_TOKEN, return_value="signing-key")
        p.start()
        self.addCleanup(p.stop)

    def test_url_is_host_loopback(self) -> None:
        self.assertEqual("http://127.0.0.1:8099", self.orch.url())

    def test_socket_shared_client_uses_explicit_host_and_open_bind(self) -> None:
        orch = DockerOrchestrator(
            port=8099, client_host="172.17.0.1", root_mount_source="state-volume"
        )
        with patch(_TOKEN, return_value="k"), \
                patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, return_value=_proc()) as run, patch(_SLEEP):
            orch.ensure_running()
        self.assertEqual("http://172.17.0.1:8099", orch.url())
        argv = next(c.args[0] for c in run.call_args_list
                    if c.args[0][:2] == ["docker", "run"])
        self.assertEqual("0.0.0.0:8099:8099", argv[argv.index("--publish") + 1])
        self.assertIn("state-volume:/bot-bottle-root", argv)

    def test_host_path_and_named_root_mount_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "host_root or root_mount_source"):
            DockerOrchestrator(
                host_root=Path("/host/path"),
                root_mount_source="state-volume",
            )

    def test_socket_shared_job_network_uses_container_dns(self) -> None:
        orch = DockerOrchestrator(
            name="orchestrator-itest",
            port=22001,
            client_network="runner-job-network",
            root_mount_source="state-volume",
        )
        with patch(_TOKEN, return_value="k"), \
                patch(
                    _URLOPEN,
                    side_effect=[urllib.error.URLError("down"), _health(200)],
                ), patch(_RUN, return_value=_proc()) as run, patch(_SLEEP):
            orch.ensure_running()
        self.assertEqual("http://orchestrator-itest:8099", orch.url())
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            [
                "docker", "network", "connect",
                "runner-job-network", "orchestrator-itest",
            ],
            calls,
        )
        argv = next(call for call in calls if call[:2] == ["docker", "run"])
        self.assertEqual(
            "127.0.0.1:22001:8099",
            argv[argv.index("--publish") + 1],
        )

    def test_gateway_url_is_the_container_dns_name(self) -> None:
        # The gateway reaches the orchestrator by name on the control network.
        self.assertEqual(f"http://{ORCHESTRATOR_NAME}:8099", self.orch.gateway_url())

    def test_is_healthy_probes_health_on_the_loopback_url(self) -> None:
        with patch(_URLOPEN, return_value=_health(200)):
            self.assertTrue(self.orch.is_healthy())
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            self.assertFalse(self.orch.is_healthy())

    def test_noop_when_healthy_and_source_unchanged(self) -> None:
        # A healthy orchestrator on current source is left alone — recreating it
        # on every launch drops in-memory egress tokens (#381).
        current = source_hash(self.orch._repo_root)

        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout=current)
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.orch.ensure_running()
        self.assertEqual([], [c.args[0] for c in run.call_args_list
                              if c.args[0][:2] == ["docker", "run"]])

    def test_recreates_when_source_changed(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout="stale-hash")
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.orch.ensure_running()
        runs = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        self.assertIn(ORCHESTRATOR_NAME, runs[0])
        current = source_hash(self.orch._repo_root)
        self.assertIn(f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current}", runs[0])

    def test_starts_when_absent_on_control_net_and_loopback(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.orch.ensure_running()
        argv = next(c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"])
        self.assertIn(ORCHESTRATOR_NAME, argv)
        # Control network only — agents are never on it.
        self.assertEqual(ORCHESTRATOR_NETWORK, argv[argv.index("--network") + 1])
        # Published on loopback for the CLI, mapped to the fixed internal 8099.
        self.assertEqual("127.0.0.1:8099:8099", argv[argv.index("--publish") + 1])
        # The lean control plane: no mitmproxy CA mount, no gateway daemons.
        self.assertFalse([a for a in argv if a.endswith(":/home/mitmproxy/.mitmproxy")])
        self.assertNotIn("BOT_BOTTLE_GATEWAY_DAEMONS", " ".join(argv))
        self.assertNotIn("/bot-bottle-src", " ".join(argv))
        self.assertNotIn("PYTHONPATH", " ".join(argv))
        # Orchestrator entrypoint args (image ENTRYPOINT is `-m bot_bottle.orchestrator`).
        self.assertIn("--broker", argv)
        self.assertIn("stub", argv)

    def test_publish_maps_host_port_to_fixed_internal_port(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        orch = DockerOrchestrator(port=20001)
        with patch(_TOKEN, return_value="k"), \
                patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            orch.ensure_running()
        argv = next(c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"])
        self.assertEqual("127.0.0.1:20001:8099", argv[argv.index("--publish") + 1])

    def test_raises_on_startup_timeout(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")), \
                patch(_RUN, return_value=_proc()), \
                patch(_SLEEP), patch(_MONOTONIC, side_effect=[0.0, 0.5, 2.0]):
            with self.assertRaises(OrchestratorStartError):
                self.orch.ensure_running(startup_timeout=1.0)

    def test_noop_when_healthy_and_inspect_fails(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(returncode=1, stderr="daemon error")
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.orch.ensure_running()
        self.assertEqual([], [c.args[0] for c in run.call_args_list
                              if c.args[0][:2] == ["docker", "run"]])

    def test_creates_control_network_internal(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(returncode=1, stderr="not found")
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.orch.ensure_running()
        creates = [c.args[0] for c in run.call_args_list if c.args[0][:3] == ["docker", "network", "create"]]
        control = [c for c in creates if ORCHESTRATOR_NETWORK in c]
        self.assertEqual(1, len(control))
        self.assertIn("--internal", control[0])

    def test_ensure_built_builds_the_orchestrator_image(self) -> None:
        with patch(_RUN, return_value=_proc()) as run:
            self.orch.ensure_built()
        builds = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "build"]]
        self.assertEqual(1, len(builds))
        self.assertTrue(any(a.endswith("Dockerfile.orchestrator") for a in builds[0]))
        arg_index = builds[0].index("--build-arg")
        self.assertTrue(
            builds[0][arg_index + 1].startswith("PYTHON_BASE_IMAGE=python:"),
        )

    def test_ensure_built_raises_on_build_failure(self) -> None:
        with patch(_RUN, return_value=_proc(returncode=1, stderr="no space left")):
            with self.assertRaises(GatewayError):
                self.orch.ensure_built()

    def test_ensure_built_pulls_without_dockerfile(self) -> None:
        orch = DockerOrchestrator("registry/orchestrator@sha256:" + "a" * 64,
                                  dockerfile=None)
        with patch(_RUN, return_value=_proc()) as run:
            orch.ensure_built()
        run.assert_called_once_with(["docker", "pull", orch.image_ref])

    def test_is_running_reads_docker_ps(self) -> None:
        with patch(_RUN, return_value=_proc(stdout=ORCHESTRATOR_NAME)):
            self.assertTrue(self.orch.is_running())
        with patch(_RUN, return_value=_proc(stdout="")):
            self.assertFalse(self.orch.is_running())

    def test_stop_removes_the_container(self) -> None:
        with patch(_RUN) as run:
            self.orch.stop()
        argv = run.call_args.args[0]
        self.assertEqual(["docker", "rm", "--force", ORCHESTRATOR_NAME], argv)


if __name__ == "__main__":
    unittest.main()
