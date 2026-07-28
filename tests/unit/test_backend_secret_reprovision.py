"""Backend-agnostic and backend-specific encrypted-secret recovery tests."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot_bottle.orchestrator.reprovision import reprovision_bottles
from bot_bottle.backend.firecracker import consolidated_launch as fc
from bot_bottle.backend.macos_container import consolidated_launch as mac
from bot_bottle.backend.docker import consolidated_launch as docker
from bot_bottle.orchestrator.client import OrchestratorClientError


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TestSharedReprovision(unittest.TestCase):
    def test_joins_registry_records_by_source_ip(self) -> None:
        client = Mock()
        client.list_bottles.return_value = [
            {"bottle_id": "b1", "source_ip": "10.0.0.1"},
            {"bottle_id": "b2", "source_ip": "10.0.0.2"},
            {"bottle_id": 3, "source_ip": "10.0.0.3"},
        ]
        client.reprovision_gateway.side_effect = [True, False]
        count = reprovision_bottles(
            client, {"10.0.0.1": " key-1\n", "10.0.0.2": "key-2"},
        )
        self.assertEqual(1, count)
        self.assertEqual(
            [("b1", "key-1"), ("b2", "key-2")],
            [call.args for call in client.reprovision_gateway.call_args_list],
        )

    def test_one_failure_does_not_block_other_bottles(self) -> None:
        client = Mock()
        client.list_bottles.return_value = [
            {"bottle_id": "b1", "source_ip": "10.0.0.1"},
            {"bottle_id": "b2", "source_ip": "10.0.0.2"},
        ]
        client.reprovision_gateway.side_effect = [
            OrchestratorClientError("bad key"), True,
        ]
        with patch("bot_bottle.orchestrator.reprovision.debug") as debug:
            count = reprovision_bottles(
                client, {"10.0.0.1": "key-1", "10.0.0.2": "key-2"},
            )
        self.assertEqual(1, count)
        self.assertEqual("b1", debug.call_args.kwargs["context"]["bottle_id"])
        self.assertNotIn("bad key", repr(debug.call_args))


class TestMacosReprovision(unittest.TestCase):
    def test_reads_configured_container_env_and_reprovisions(self) -> None:
        endpoint = mac.GatewayEndpoint("http://orch", "10.0.0.9", "PEM", "net")
        agent = SimpleNamespace(slug="demo")
        client = Mock()
        with patch.object(mac, "enumerate_active", return_value=[agent]), \
             patch.object(mac.container_mod, "inspect_container_network_ip",
                          return_value="10.0.0.1"), \
             patch.object(mac.container_mod, "read_container_env", return_value="key"), \
             patch.object(mac, "OrchestratorClient", return_value=client), \
             patch.object(mac, "reprovision_bottles", return_value=1) as restore, \
             patch.object(mac, "info"):
            mac._reprovision_running_bottles(endpoint)
        restore.assert_called_once_with(client, {"10.0.0.1": "key"})

    def test_enumeration_failure_is_best_effort(self) -> None:
        endpoint = mac.GatewayEndpoint("http://orch", "10.0.0.9", "PEM", "net")
        with patch.object(mac, "enumerate_active",
                          side_effect=mac.EnumerationError("failed")), \
             patch.object(mac, "info") as info:
            mac._reprovision_running_bottles(endpoint)
        self.assertIn("skipped", info.call_args.args[0])


class TestDockerReprovision(unittest.TestCase):
    def test_maps_network_containers_to_keys(self) -> None:
        # The gateway container (excluded — it's on the data network but isn't
        # an agent) + one agent + a malformed line.
        inspect = _proc(stdout=(
            "bot-bottle-orch-gateway 172.18.0.2/16\n"
            "bot-bottle-a 172.18.0.3/16\n"
            "malformed\n"
        ))
        key = _proc(stdout="secret\n")
        client = Mock()
        with patch.object(docker, "OrchestratorClient", return_value=client), \
             patch.object(docker, "run_docker", side_effect=[inspect, key]), \
             patch.object(docker, "reprovision_bottles", return_value=1) as restore, \
             patch.object(docker.log, "info"):
            docker._reprovision_running_bottles("http://orch")
        restore.assert_called_once_with(client, {"172.18.0.3": "secret"})

    def test_missing_docker_is_best_effort(self) -> None:
        with patch.object(docker, "run_docker", side_effect=FileNotFoundError("docker")), \
             patch.object(docker.log, "info") as info:
            docker._reprovision_running_bottles("http://orch")
        self.assertIn("skipped", info.call_args.args[0])


class TestFirecrackerReprovision(unittest.TestCase):
    def test_persists_key_over_stdin_not_argv(self) -> None:
        with patch.object(fc.util, "ssh_base_argv", return_value=["ssh", "guest"]), \
             patch.object(fc.subprocess, "run", return_value=_proc()) as run:
            fc.persist_env_var_secret(Path("/key"), "10.0.0.1", "super-secret")
        self.assertEqual("super-secret", run.call_args.kwargs["input"])
        self.assertNotIn("super-secret", " ".join(run.call_args.args[0]))

    def test_persist_failure_is_fatal_to_launch(self) -> None:
        with patch.object(fc.util, "ssh_base_argv", return_value=["ssh", "guest"]), \
             patch.object(fc.subprocess, "run", return_value=_proc(1, stderr="denied")):
            with self.assertRaisesRegex(fc.ConsolidatedLaunchError, "denied"):
                fc.persist_env_var_secret(Path("/key"), "10.0.0.1", "secret")


if __name__ == "__main__":
    unittest.main()
