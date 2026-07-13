"""Unit tests for the consolidated Docker sidecar (PRD 0070). Docker mocked."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from bot_bottle.orchestrator.sidecar import (
    SIDECAR_NAME,
    DockerSidecar,
    SidecarError,
)

_RUN_DOCKER = "bot_bottle.orchestrator.sidecar.run_docker"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestDockerSidecar(unittest.TestCase):
    def setUp(self) -> None:
        self.sc = DockerSidecar("bot-bottle-sidecars:latest")

    def test_default_name(self) -> None:
        self.assertEqual(SIDECAR_NAME, self.sc.name)

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

    def test_ensure_running_raises_on_docker_failure(self) -> None:
        def fake(argv: list[str]) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:2] == ["docker", "run"]:
                return _proc(returncode=1, stderr="boom")
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            with self.assertRaises(SidecarError):
                self.sc.ensure_running()

    def test_stop_is_idempotent_on_missing(self) -> None:
        absent = _proc(returncode=1, stderr="Error: No such container: x")
        with patch(_RUN_DOCKER, return_value=absent):
            self.sc.stop()  # must not raise

    def test_stop_raises_on_other_failure(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1, stderr="daemon down")):
            with self.assertRaises(SidecarError):
                self.sc.stop()


if __name__ == "__main__":
    unittest.main()
