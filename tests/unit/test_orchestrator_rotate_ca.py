"""Unit: the `rotate_ca` one-shot CLI rotates every backend's gateway CA
(issue #450)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.gateway import GatewayError
from bot_bottle.orchestrator import rotate_ca


def _service(ca_dir: Path | None, *, stop_error: Exception | None = None,
             still_running: bool = False) -> MagicMock:
    service = MagicMock()
    gateway = service.gateway.return_value
    gateway.persisted_ca_dir.return_value = ca_dir
    gateway.stop.side_effect = stop_error
    gateway.is_running.return_value = still_running
    return service


def _seed(d: str) -> Path:
    (Path(d) / "mitmproxy-ca.pem").write_text("x")
    (Path(d) / "mitmproxy-ca-cert.pem").write_text("x")
    return Path(d)


def _ca_files(d: Path) -> list[Path]:
    return list(d.glob("mitmproxy-ca*"))


class TestRotateCaCli(unittest.TestCase):
    def _main(self, services: list[tuple[str, MagicMock]]) -> int:
        with patch.object(rotate_ca, "_services",
                          return_value=[(n, lambda s=s: s) for n, s in services]):
            return rotate_ca.main([])

    def test_clears_each_backends_ca_and_stops_its_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as shared, tempfile.TemporaryDirectory() as fc:
            docker, firecracker = _service(_seed(shared)), _service(_seed(fc))
            self.assertEqual(0, self._main([("docker", docker), ("firecracker", firecracker)]))
            # Persisted CAs are gone, so the next start mints new ones, and no
            # gateway keeps serving the old CA from memory.
            self.assertEqual([], _ca_files(Path(shared)) + _ca_files(Path(fc)))
        docker.gateway.return_value.stop.assert_called_once()
        firecracker.gateway.return_value.stop.assert_called_once()

    def test_runs_under_every_backends_bring_up_lock(self) -> None:
        # A concurrent launch must not restart a gateway between stop and clear.
        with tempfile.TemporaryDirectory() as d:
            service = _service(_seed(d))
            lock = service.bring_up_lock.return_value

            def cleared_before_release(*_: object) -> None:
                self.assertEqual([], _ca_files(Path(d)))

            lock.__exit__.side_effect = cleared_before_release
            self.assertEqual(0, self._main([("docker", service)]))
        lock.__enter__.assert_called_once()

    def test_a_failed_stop_keeps_its_ca_and_the_rest_still_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as dk, tempfile.TemporaryDirectory() as fc:
            docker = _service(_seed(dk), stop_error=GatewayError("daemon wedged"))
            firecracker = _service(_seed(fc))
            self.assertEqual(1, self._main([("docker", docker), ("firecracker", firecracker)]))
            self.assertEqual(2, len(_ca_files(Path(dk))))  # still serving it: keep it
            self.assertEqual([], _ca_files(Path(fc)))

    def test_a_shared_ca_dir_is_kept_unless_every_gateway_using_it_stopped(self) -> None:
        # docker + macOS share ~/.bot-bottle/gateway-ca: clearing it under a
        # gateway that is still up leaves it serving a CA nobody can fetch.
        with tempfile.TemporaryDirectory() as shared:
            docker = _service(_seed(shared), stop_error=GatewayError("daemon wedged"))
            macos = _service(Path(shared))
            self.assertEqual(1, self._main([("docker", docker), ("macos-container", macos)]))
            self.assertEqual(2, len(_ca_files(Path(shared))))
        macos.gateway.return_value.stop.assert_called_once()

    def test_a_gateway_still_running_after_stop_counts_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(1, self._main([("macos-container",
                                             _service(_seed(d), still_running=True))]))
            self.assertEqual(2, len(_ca_files(Path(d))))

    def test_named_volume_ca_still_stops_the_gateway(self) -> None:
        service = _service(None)
        self.assertEqual(0, self._main([("docker", service)]))
        service.gateway.return_value.stop.assert_called_once()

    def test_every_available_registered_backend_is_covered(self) -> None:
        # Driven by the backend registry: a new backend needs no change here.
        backend = MagicMock()

        def available(name: str) -> bool:
            return name == "b"

        with patch("bot_bottle.backend.known_backend_names", return_value=("a", "b")), \
                patch("bot_bottle.backend.has_backend", side_effect=available), \
                patch("bot_bottle.backend.get_bottle_backend", return_value=backend) as get:
            services = rotate_ca._services()  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(["b"], [name for name, _ in services])
            self.assertIs(backend.infra_service.return_value, services[0][1]())
        get.assert_called_once_with("b")

    def test_no_backend_available_is_a_noop(self) -> None:
        self.assertEqual(0, self._main([]))


class TestPersistedCaDir(unittest.TestCase):
    def test_docker_bind_mount_is_a_dir_but_a_named_volume_is_not(self) -> None:
        from bot_bottle.backend.docker.gateway import DockerGateway
        self.assertEqual(Path("/ca"), DockerGateway(ca_mount_source="/ca").persisted_ca_dir())
        self.assertIsNone(DockerGateway(ca_mount_source="ci-ca-volume").persisted_ca_dir())

    def test_firecracker_uses_its_own_infra_dir(self) -> None:
        from bot_bottle.backend.firecracker import infra_vm
        from bot_bottle.backend.firecracker.gateway import FirecrackerGateway
        with tempfile.TemporaryDirectory() as td, \
                patch.object(infra_vm, "_infra_dir", return_value=Path(td)):
            self.assertEqual(Path(td) / "gateway-ca", FirecrackerGateway().persisted_ca_dir())


if __name__ == "__main__":
    unittest.main()
