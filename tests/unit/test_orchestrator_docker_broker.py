"""Unit tests for the Docker launch broker (PRD 0070). Docker is mocked."""

from __future__ import annotations

import secrets
import unittest
from unittest.mock import Mock, patch

from bot_bottle.orchestrator.broker import (
    BrokerAuthError,
    LaunchRequest,
    sign_request,
)
from bot_bottle.control.docker_broker import (
    BOTTLE_ID_LABEL,
    DockerBroker,
    DockerBrokerError,
    container_name,
    rm_argv,
    run_argv,
)


class TestArgv(unittest.TestCase):
    def test_run_argv_uses_only_static_fields(self) -> None:
        req = LaunchRequest(op="launch", bottle_id="b1", image_ref="busybox")
        argv = run_argv(req)
        self.assertEqual(["docker", "run"], argv[:2])
        self.assertIn("--name", argv)
        self.assertIn(container_name("b1"), argv)
        self.assertIn(f"{BOTTLE_ID_LABEL}=b1", argv)
        self.assertEqual("busybox", argv[-1])  # image is the terminal arg

    def test_rm_argv(self) -> None:
        req = LaunchRequest(op="teardown", bottle_id="b1")
        self.assertEqual(["docker", "rm", "--force", container_name("b1")], rm_argv(req))

    def test_container_name_is_prefixed(self) -> None:
        name = container_name("b1")
        self.assertTrue(name.startswith("bot-bottle-orch-"))
        self.assertTrue(name.endswith("b1"))


class TestDockerBroker(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = secrets.token_bytes(16)
        self.broker = DockerBroker(self.secret)

    def _submit(self, req: LaunchRequest) -> None:
        self.broker.submit(sign_request(req, self.secret))

    def test_launch_invokes_docker_run(self) -> None:
        req = LaunchRequest(op="launch", bottle_id="b1", image_ref="busybox")
        with patch.object(self.broker, "_docker", return_value=Mock(returncode=0, stderr="")) as m:
            self._submit(req)
        m.assert_called_once()
        self.assertEqual(run_argv(req), m.call_args.args[0])

    def test_launch_without_image_raises_and_skips_docker(self) -> None:
        req = LaunchRequest(op="launch", bottle_id="b1", image_ref="")
        with patch.object(self.broker, "_docker") as m:
            with self.assertRaises(DockerBrokerError):
                self._submit(req)
            m.assert_not_called()

    def test_launch_docker_failure_raises(self) -> None:
        req = LaunchRequest(op="launch", bottle_id="b1", image_ref="busybox")
        with patch.object(self.broker, "_docker", return_value=Mock(returncode=1, stderr="boom")):
            with self.assertRaises(DockerBrokerError):
                self._submit(req)

    def test_teardown_invokes_docker_rm(self) -> None:
        req = LaunchRequest(op="teardown", bottle_id="b1")
        with patch.object(self.broker, "_docker", return_value=Mock(returncode=0, stderr="")) as m:
            self._submit(req)
        self.assertEqual(rm_argv(req), m.call_args.args[0])

    def test_teardown_is_idempotent_on_missing_container(self) -> None:
        req = LaunchRequest(op="teardown", bottle_id="b1")
        absent = Mock(returncode=1, stderr="Error: No such container: bot-bottle-orch-b1")
        with patch.object(self.broker, "_docker", return_value=absent):
            self._submit(req)  # must not raise

    def test_teardown_other_failure_raises(self) -> None:
        req = LaunchRequest(op="teardown", bottle_id="b1")
        with patch.object(self.broker, "_docker", return_value=Mock(returncode=1, stderr="daemon down")):
            with self.assertRaises(DockerBrokerError):
                self._submit(req)

    def test_forged_token_never_touches_docker(self) -> None:
        req = LaunchRequest(op="launch", bottle_id="b1", image_ref="busybox")
        forged = sign_request(req, secrets.token_bytes(16))
        with patch.object(self.broker, "_docker") as m:
            with self.assertRaises(BrokerAuthError):
                self.broker.submit(forged)
            m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
