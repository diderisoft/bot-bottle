"""Unit: the `rotate_ca` one-shot CLI rotates every backend's gateway CA
(issue #450)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.orchestrator import rotate_ca


def _gateway(ca_dir: Path | None) -> MagicMock:
    gateway = MagicMock()
    gateway.persisted_ca_dir.return_value = ca_dir
    return gateway


class TestRotateCaCli(unittest.TestCase):
    def test_clears_each_backends_ca_and_stops_its_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as shared, tempfile.TemporaryDirectory() as fc:
            for d in (shared, fc):
                (Path(d) / "mitmproxy-ca.pem").write_text("x")
                (Path(d) / "mitmproxy-ca-cert.pem").write_text("x")
            docker, firecracker = _gateway(Path(shared)), _gateway(Path(fc))
            with patch.object(rotate_ca, "_gateways", return_value=[
                ("docker", lambda: docker), ("firecracker", lambda: firecracker),
            ]):
                self.assertEqual(0, rotate_ca.main([]))
            # Persisted CAs are gone, so the next start mints new ones, and no
            # gateway keeps serving the old CA from memory.
            for d in (shared, fc):
                self.assertEqual([], list(Path(d).glob("mitmproxy-ca*")))
        docker.stop.assert_called_once()
        firecracker.stop.assert_called_once()

    def test_named_volume_ca_still_stops_the_gateway(self) -> None:
        gateway = _gateway(None)
        with patch.object(rotate_ca, "_gateways", return_value=[("docker", lambda: gateway)]):
            self.assertEqual(0, rotate_ca.main([]))
        gateway.stop.assert_called_once()

    def test_every_available_registered_backend_is_covered(self) -> None:
        # Driven by the backend registry: a new backend needs no change here.
        backend = MagicMock()

        def available(name: str) -> bool:
            return name == "b"

        with patch("bot_bottle.backend.known_backend_names", return_value=("a", "b")), \
                patch("bot_bottle.backend.has_backend", side_effect=available), \
                patch("bot_bottle.backend.get_bottle_backend", return_value=backend) as get:
            gateways = rotate_ca._gateways()  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(["b"], [name for name, _ in gateways])
            self.assertIs(backend.infra_service.return_value.gateway.return_value,
                          gateways[0][1]())
        get.assert_called_once_with("b")

    def test_no_backend_available_is_a_noop(self) -> None:
        with patch.object(rotate_ca, "_gateways", return_value=[]):
            self.assertEqual(0, rotate_ca.main([]))


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
