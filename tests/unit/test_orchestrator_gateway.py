"""Unit tests for the consolidated Docker gateway (PRD 0070). Docker mocked."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from bot_bottle.orchestrator.gateway import (
    GATEWAY_CA_CERT,
    GATEWAY_NAME,
    DockerGateway,
    GatewayError,
)


_CA_PEM = "-----BEGIN CERTIFICATE-----\nMII...\n-----END CERTIFICATE-----\n"

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
        # Persists its CA on a named volume so agents keep trusting it.
        self.assertTrue(any("mitmproxy" in a for a in runs[0]))

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

    def test_ca_cert_pem_reads_from_container(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(stdout=_CA_PEM)) as m:
            self.assertEqual(_CA_PEM, self.sc.ca_cert_pem())
        argv = m.call_args.args[0]
        self.assertEqual(["docker", "exec", self.sc.name, "cat", GATEWAY_CA_CERT], argv)

    def test_ca_cert_pem_raises_when_absent(self) -> None:
        # timeout=0 → one probe then give up (no polling delay in the test).
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1, stderr="No such file")):
            with self.assertRaises(GatewayError):
                self.sc.ca_cert_pem(timeout=0)

    def test_ca_cert_pem_polls_until_mitmproxy_writes_it(self) -> None:
        # First read: CA not there yet; second read: present.
        seq = [_proc(returncode=1, stderr="No such file"), _proc(stdout=_CA_PEM)]
        with patch(_RUN_DOCKER, side_effect=seq), \
                patch("bot_bottle.orchestrator.gateway.time.sleep"):
            self.assertEqual(_CA_PEM, self.sc.ca_cert_pem(timeout=5))

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

    def test_ensure_built_builds_even_when_image_present(self) -> None:
        # Always build (cache-aware) so a flat-source change rebuilds; the old
        # build-if-missing silently ran a stale single-tenant image.
        calls: list[list[str]] = []

        def rec(argv: list[str]) -> Mock:
            calls.append(argv)
            return _proc()  # image present, build succeeds

        with patch(_RUN_DOCKER, side_effect=rec):
            self.sc.ensure_built()
        builds = [c for c in calls if c[:2] == ["docker", "build"]]
        self.assertEqual(1, len(builds))
        self.assertIn(self.sc.image_ref, builds[0])
        self.assertTrue(any(a.endswith("Dockerfile.sidecars") for a in builds[0]))
        self.assertNotIn("--no-cache", builds[0])

    def test_ensure_built_no_cache_env_forces_full_rebuild(self) -> None:
        calls: list[list[str]] = []

        def rec(argv: list[str]) -> Mock:
            calls.append(argv)
            return _proc()

        with patch(_RUN_DOCKER, side_effect=rec), \
                patch.dict("os.environ", {"BOT_BOTTLE_NO_CACHE": "1"}):
            self.sc.ensure_built()
        builds = [c for c in calls if c[:2] == ["docker", "build"]]
        self.assertIn("--no-cache", builds[0])

    def test_ensure_built_noop_when_no_dockerfile(self) -> None:
        sc = DockerGateway("busybox", dockerfile=None)
        with patch(_RUN_DOCKER) as m:
            sc.ensure_built()
        m.assert_not_called()

    def test_ensure_built_raises_on_build_failure(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1, stderr="build boom")):
            with self.assertRaises(GatewayError):
                self.sc.ensure_built()


if __name__ == "__main__":
    unittest.main()
