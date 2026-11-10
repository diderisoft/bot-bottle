"""Unit: the `rotate_ca` one-shot CLI rotates every backend's gateway CA
(issue #450, PRD 0090)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.control.gateway import GatewayError, GatewayProvisionError
from bot_bottle.control import rotate_ca


def _service(ca_dir: Path | None, *, running: bool = False, stays_running: bool = False,
             roll_error: Exception | None = None,
             ensure_error: BaseException | None = None) -> MagicMock:
    """An infra service whose gateway is up (`running`) until stopped."""
    state = {"running": running}
    service = MagicMock()
    gateway = service.gateway.return_value
    gateway.persisted_ca_dir.return_value = ca_dir
    gateway.is_running.side_effect = lambda: state["running"]
    gateway.roll_over_ca.side_effect = roll_error

    def stop() -> None:
        state["running"] = stays_running

    gateway.stop.side_effect = stop
    service.ensure_running.side_effect = ensure_error
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

    def test_a_running_gateway_is_rolled_over_restarted_and_reattached(self) -> None:
        # Running agents keep validating via the cross-signature, so the CA is
        # rolled over in place rather than deleted.
        with tempfile.TemporaryDirectory() as d:
            service = _service(_seed(d), running=True)
            self.assertEqual(0, self._main([("macos-container", service)]))
            self.assertEqual(2, len(_ca_files(Path(d))))  # not deleted
        gateway = service.gateway.return_value
        gateway.roll_over_ca.assert_called_once()
        gateway.stop.assert_called_once()
        # Brought up to date before the rollover (it runs the gateway's own
        # code), then restarted on the new CA.
        self.assertEqual(2, service.ensure_running.call_count)

    def test_the_gateway_is_brought_up_to_date_before_rolling_over(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            service = _service(_seed(d), running=True)
            order: list[str] = []
            service.ensure_running.side_effect = lambda: order.append("ensure")
            service.gateway.return_value.roll_over_ca.side_effect = lambda: order.append("roll")
            self.assertEqual(0, self._main([("docker", service)]))
        self.assertEqual(["ensure", "roll", "ensure"], order)

    def test_a_stopped_gateway_has_its_persisted_ca_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            service = _service(_seed(d))
            self.assertEqual(0, self._main([("docker", service)]))
            self.assertEqual([], _ca_files(Path(d)))
        service.gateway.return_value.roll_over_ca.assert_not_called()
        service.ensure_running.assert_not_called()

    def test_a_shared_ca_dir_rolls_over_once_and_restarts_every_running_user(self) -> None:
        # docker + macOS share ~/.bot-bottle/gateway-ca.
        with tempfile.TemporaryDirectory() as shared:
            docker = _service(_seed(shared), running=True)
            macos = _service(Path(shared), running=True)
            self.assertEqual(0, self._main([("docker", docker), ("macos-container", macos)]))
            self.assertEqual(2, len(_ca_files(Path(shared))))
        rolled = [s.gateway.return_value.roll_over_ca.call_count for s in (docker, macos)]
        self.assertEqual(1, sum(rolled))
        self.assertEqual(2, docker.ensure_running.call_count)  # rolled over through it
        macos.ensure_running.assert_called_once()

    def test_a_shared_dir_with_one_user_running_is_rolled_over_not_cleared(self) -> None:
        # Clearing it for the stopped docker gateway would throw away the
        # rollover the running macOS gateway just wrote there.
        with tempfile.TemporaryDirectory() as shared:
            docker = _service(_seed(shared))
            macos = _service(Path(shared), running=True)
            self.assertEqual(0, self._main([("docker", docker), ("macos-container", macos)]))
            self.assertEqual(2, len(_ca_files(Path(shared))))
        macos.gateway.return_value.roll_over_ca.assert_called_once()
        docker.ensure_running.assert_not_called()

    def test_runs_under_every_backends_bring_up_lock(self) -> None:
        # A concurrent launch must not restart a gateway mid-rotation.
        with tempfile.TemporaryDirectory() as d:
            service = _service(_seed(d), running=True)
            lock = service.bring_up_lock.return_value

            def rotated_before_release(*_: object) -> None:
                self.assertEqual(2, service.ensure_running.call_count)

            lock.__exit__.side_effect = rotated_before_release
            self.assertEqual(0, self._main([("docker", service)]))
        lock.__enter__.assert_called_once()

    def test_a_failed_group_keeps_its_ca_and_the_rest_still_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as dk, tempfile.TemporaryDirectory() as fc:
            docker = _service(_seed(dk), running=True,
                              roll_error=GatewayError("could not roll the gateway CA over"))
            firecracker = _service(_seed(fc))
            self.assertEqual(1, self._main([("docker", docker), ("firecracker", firecracker)]))
            self.assertEqual(2, len(_ca_files(Path(dk))))
            self.assertEqual([], _ca_files(Path(fc)))
        docker.gateway.return_value.stop.assert_not_called()  # still serving its CA

    def test_a_gateway_still_running_after_stop_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            service = _service(_seed(d), running=True, stays_running=True)
            self.assertEqual(1, self._main([("macos-container", service)]))
        service.ensure_running.assert_called_once()  # the update, never the restart

    def test_a_failed_reattach_is_reported_not_fatal(self) -> None:
        # ensure_running ends a failed STRICT reattach with die() (SystemExit).
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            broken = _service(_seed(a), running=True, ensure_error=SystemExit(1))
            fine = _service(_seed(b), running=True)
            self.assertEqual(1, self._main([("docker", broken), ("firecracker", fine)]))
        broken.gateway.return_value.roll_over_ca.assert_not_called()
        fine.gateway.return_value.roll_over_ca.assert_called_once()
        self.assertEqual(2, fine.ensure_running.call_count)

    def test_a_running_gateway_on_a_named_volume_can_now_be_rotated(self) -> None:
        service = _service(None, running=True)
        self.assertEqual(0, self._main([("docker", service)]))
        service.gateway.return_value.roll_over_ca.assert_called_once()

    def test_a_stopped_gateway_on_a_named_volume_is_reported(self) -> None:
        service = _service(None)
        self.assertEqual(0, self._main([("docker", service)]))
        service.gateway.return_value.roll_over_ca.assert_not_called()

    def test_every_available_registered_backend_is_covered(self) -> None:
        # Driven by the backend registry: a new backend needs no change here.
        backend = MagicMock()

        def available(_settings: object, name: str) -> bool:
            return name == "b"

        with patch("bot_bottle.backend.known_backend_names", return_value=("a", "b")), \
                patch("bot_bottle.backend.has_backend", side_effect=available), \
                patch("bot_bottle.backend.get_bottle_backend", return_value=backend) as get:
            settings = Settings()
            services = rotate_ca._services(settings)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(["b"], [name for name, _ in services])
            self.assertIs(backend.infra_service.return_value, services[0][1]())
        get.assert_called_once_with(settings, "b")

    def test_no_backend_available_is_a_noop(self) -> None:
        self.assertEqual(0, self._main([]))


class TestGatewayRollOver(unittest.TestCase):
    def test_runs_the_rollover_in_the_gateway_with_a_known_path(self) -> None:
        from bot_bottle.backend.docker.gateway import DockerGateway
        gateway = DockerGateway(settings=Settings())
        with patch.object(gateway, "provisioning_transport") as transport:
            gateway.roll_over_ca()
        argv = transport.return_value.exec.call_args.args[0]
        self.assertEqual("/usr/bin/env", argv[0])
        self.assertTrue(argv[1].startswith("PATH="))
        self.assertEqual(["python3", "-m", "bot_bottle.gateway.ca_rollover",
                          "/home/mitmproxy/.mitmproxy"], argv[2:])

    def test_a_failed_exec_is_a_gateway_error(self) -> None:
        from bot_bottle.backend.macos_container.gateway import MacosGateway
        gateway = MacosGateway(settings=Settings())
        with patch.object(gateway, "provisioning_transport") as transport:
            transport.return_value.exec.side_effect = GatewayProvisionError("exit 1")
            with self.assertRaisesRegex(GatewayError, "roll the gateway CA over"):
                gateway.roll_over_ca()

    def test_firecracker_saves_the_rolled_over_ca_to_the_host(self) -> None:
        # Its rootfs is rebuilt every boot: only the host copy survives.
        from bot_bottle.backend.firecracker.gateway import FirecrackerGateway
        gateway = FirecrackerGateway(Settings())
        with patch.object(gateway, "provisioning_transport"), \
                patch.object(gateway, "_save_ca") as save:
            gateway.roll_over_ca()
        save.assert_called_once()


class TestPersistedCaDir(unittest.TestCase):
    def test_docker_bind_mount_is_a_dir_but_a_named_volume_is_not(self) -> None:
        from bot_bottle.backend.docker.gateway import DockerGateway
        self.assertEqual(Path("/ca"), DockerGateway(ca_mount_source="/ca", settings=Settings()).persisted_ca_dir())
        self.assertIsNone(DockerGateway(ca_mount_source="ci-ca-volume", settings=Settings()).persisted_ca_dir())

    def test_firecracker_keeps_its_ca_under_the_root_not_the_cache(self) -> None:
        from bot_bottle.backend.firecracker import infra_vm
        from bot_bottle.backend.firecracker.gateway import FirecrackerGateway
        with tempfile.TemporaryDirectory() as td, \
                patch.object(infra_vm, "state_dir", return_value=Path(td)):
            self.assertEqual(Path(td) / "gateway-ca", FirecrackerGateway(Settings()).persisted_ca_dir())


if __name__ == "__main__":
    unittest.main()
