"""Host container-engine detection (optional; used for firecracker overlay builds)."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend import container_engine as ce


def _ok(*_a: object, **_k: object) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], 0, "", "")


class HostContainerEngine(unittest.TestCase):
    def test_prefers_docker_when_both_are_usable(self) -> None:
        with patch.dict("os.environ", {}, clear=False), \
             patch.object(ce.shutil, "which", return_value="/bin/x"), \
             patch.object(ce.subprocess, "run", side_effect=_ok):
            self.assertEqual("docker", ce.host_container_engine())

    def test_falls_back_to_podman(self) -> None:
        def which(name: str) -> str | None:
            return "/bin/podman" if name == "podman" else None
        with patch.object(ce.shutil, "which", side_effect=which), \
             patch.object(ce.subprocess, "run", side_effect=_ok):
            self.assertEqual("podman", ce.host_container_engine())

    def test_unreachable_docker_daemon_is_not_usable(self) -> None:
        def run(argv: list[str], **_k: object) -> "subprocess.CompletedProcess[str]":
            return subprocess.CompletedProcess(argv, 1 if argv[0] == "docker" else 0, "", "")
        with patch.object(ce.shutil, "which", return_value="/bin/x"), \
             patch.object(ce.subprocess, "run", side_effect=run):
            self.assertEqual("podman", ce.host_container_engine())

    def test_probe_timeout_is_not_usable(self) -> None:
        with patch.object(ce.shutil, "which", return_value="/bin/x"), \
             patch.object(ce.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("docker info", 15)):
            self.assertIsNone(ce.host_container_engine())

    def test_env_vm_disables_host_builds(self) -> None:
        with patch.dict("os.environ", {ce.ENGINE_ENV: "vm"}), \
             patch.object(ce.shutil, "which", return_value="/bin/x"), \
             patch.object(ce.subprocess, "run", side_effect=_ok) as run:
            self.assertIsNone(ce.host_container_engine())
        run.assert_not_called()

    def test_env_can_force_podman(self) -> None:
        with patch.dict("os.environ", {ce.ENGINE_ENV: "podman"}), \
             patch.object(ce.shutil, "which", return_value="/bin/x"), \
             patch.object(ce.subprocess, "run", side_effect=_ok):
            self.assertEqual("podman", ce.host_container_engine())


if __name__ == "__main__":
    unittest.main()
