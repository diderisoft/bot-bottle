"""Unit tests for the consolidated Docker gateway (PRD 0070). Docker mocked."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from bot_bottle.orchestrator.gateway import (
    GATEWAY_NAME,
    DockerGateway,
    GatewayError,
)

_RUN_DOCKER = "bot_bottle.orchestrator.gateway.run_docker"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestDockerGateway(unittest.TestCase):
    def setUp(self) -> None:
        self.sc = DockerGateway("bot-bottle-sidecars:latest")

    def test_default_name(self) -> None:
        self.assertEqual(GATEWAY_NAME, self.sc.name)

    def test_is_running_reads_docker_ps(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(stdout=self.sc.name + "\n")):
            self.assertTrue(self.sc.is_running())
        with patch(_RUN_DOCKER, return_value=_proc(stdout="")):
            self.assertFalse(self.sc.is_running())

    def test_ensure_running_is_noop_when_already_up(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(stdout=self.sc.name)) as m:
            self.sc.ensure_running()
        # Only the is_running() ps probe — no rm / run.
        self.assertEqual(1, m.call_count)

    def test_ensure_running_starts_the_singleton_when_absent(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.ensure_running()

        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        self.assertIn(self.sc.name, runs[0])
        self.assertIn("bot-bottle-sidecars:latest", runs[0])
        # Runs on the shared gateway network so agents can reach it by IP.
        self.assertEqual(self.sc.network, runs[0][runs[0].index("--network") + 1])

    def test_ensure_running_creates_network_when_missing(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(returncode=1, stderr="No such network")
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.ensure_running()
        creates = [c for c in calls if c[:3] == ["docker", "network", "create"]]
        self.assertEqual([["docker", "network", "create", self.sc.network]], creates)

    def test_ensure_running_reuses_existing_network(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.ensure_running()  # network inspect returns 0 → exists
        self.assertEqual([], [c for c in calls if c[:3] == ["docker", "network", "create"]])

    def test_ensure_running_raises_on_docker_failure(self) -> None:
        def fake(argv: list[str]) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:2] == ["docker", "run"]:
                return _proc(returncode=1, stderr="boom")
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            with self.assertRaises(GatewayError):
                self.sc.ensure_running()

    def test_stop_is_idempotent_on_missing(self) -> None:
        absent = _proc(returncode=1, stderr="Error: No such container: x")
        with patch(_RUN_DOCKER, return_value=absent):
            self.sc.stop()  # must not raise

    def test_stop_raises_on_other_failure(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1, stderr="daemon down")):
            with self.assertRaises(GatewayError):
                self.sc.stop()


class TestDockerGatewayBuild(unittest.TestCase):
    def setUp(self) -> None:
        self.sc = DockerGateway()  # defaults to the real bundle image + dockerfile

    def test_image_exists_reads_docker_inspect(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=0)):
            self.assertTrue(self.sc.image_exists())
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1)):
            self.assertFalse(self.sc.image_exists())

    def test_ensure_built_is_noop_when_image_present(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=0)) as m:
            self.sc.ensure_built()
        self.assertEqual(1, m.call_count)  # only the image-inspect probe

    def test_ensure_built_builds_from_dockerfile_when_missing(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            if argv[:3] == ["docker", "image", "inspect"]:
                return _proc(returncode=1)  # missing
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.ensure_built()

        builds = [c for c in calls if c[:2] == ["docker", "build"]]
        self.assertEqual(1, len(builds))
        self.assertIn(self.sc.image_ref, builds[0])
        self.assertIn("-f", builds[0])
        self.assertTrue(any(a.endswith("Dockerfile.sidecars") for a in builds[0]))

    def test_ensure_built_noop_when_no_dockerfile(self) -> None:
        sc = DockerGateway("busybox", dockerfile=None)
        with patch(_RUN_DOCKER) as m:
            sc.ensure_built()
        m.assert_not_called()

    def test_ensure_built_raises_on_build_failure(self) -> None:
        def fake(argv: list[str]) -> Mock:
            if argv[:3] == ["docker", "image", "inspect"]:
                return _proc(returncode=1)
            return _proc(returncode=1, stderr="build boom")

        with patch(_RUN_DOCKER, side_effect=fake):
            with self.assertRaises(GatewayError):
                self.sc.ensure_built()


if __name__ == "__main__":
    unittest.main()
