"""Unit: the macOS gateway data plane as an Apple container (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.backend.macos_container.gateway import (
    GATEWAY_NAME,
    GatewayError,
    MacosGateway,
)

_GW = "bot_bottle.backend.macos_container.gateway"

_ORCH_URL = "http://192.168.128.2:8099"
_TOKEN = "gw-jwt-token"
_CA_PEM = "-----BEGIN CERTIFICATE-----\nMII...\n-----END CERTIFICATE-----\n"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def _spec(src: str, tgt: str, readonly: bool = False) -> str:
    return f"type=bind,source={src},target={tgt}" + (",readonly" if readonly else "")


class TestMacosGatewayConnect(unittest.TestCase):
    def setUp(self) -> None:
        self.gw = MacosGateway("bot-bottle-gateway:latest")

    def _connect(self, url: str = _ORCH_URL, token: str = _TOKEN) -> Mock:
        run = Mock(return_value=_proc())
        with patch(f"{_GW}.container_mod") as mod, \
                patch(f"{_GW}.host_gateway_ca_dir", return_value=Path("/ca")):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv = run
            self.gw.connect_to_orchestrator(url, token)
        return run

    def test_default_name(self) -> None:
        self.assertEqual(GATEWAY_NAME, self.gw.name)

    def test_refuses_without_orchestrator_url(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            with self.assertRaises(GatewayError):
                self.gw.connect_to_orchestrator("", _TOKEN)
            mod.run_container_argv.assert_not_called()

    def test_refuses_without_gateway_token(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            with self.assertRaises(GatewayError):
                self.gw.connect_to_orchestrator(_ORCH_URL, "")
            mod.run_container_argv.assert_not_called()

    def test_triple_homed(self) -> None:
        argv = self._connect().call_args.args[0]
        nets = [argv[i + 1] for i, a in enumerate(argv) if a == "--network"]
        self.assertEqual(
            ["bot-bottle-mac-egress", "bot-bottle-mac-gateway", "bot-bottle-mac-control"],
            nets,
        )

    def test_resolves_orchestrator_and_holds_ca(self) -> None:
        argv = self._connect().call_args.args[0]
        self.assertIn(f"BOT_BOTTLE_ORCHESTRATOR_URL={_ORCH_URL}", argv)
        self.assertIn("BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise", argv)
        self.assertIn("bot-bottle-gateway:latest", argv)
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertTrue([m for m in mounts if "target=/home/mitmproxy/.mitmproxy" in m])

    def test_injects_the_pre_minted_gateway_token(self) -> None:
        # The gateway presents the token it was handed — it never mints (holds no
        # signing key). The value rides the env, not argv.
        from bot_bottle.paths import ORCHESTRATOR_AUTH_JWT_ENV

        run = self._connect()
        env = run.call_args.kwargs["env"]
        self.assertEqual(_TOKEN, env[ORCHESTRATOR_AUTH_JWT_ENV])

    def test_raises_on_start_failure(self) -> None:
        with patch(f"{_GW}.container_mod") as mod, \
                patch(f"{_GW}.host_gateway_ca_dir", return_value=Path("/ca")):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv.return_value = _proc(returncode=1, stderr="boom")
            with self.assertRaises(GatewayError):
                self.gw.connect_to_orchestrator(_ORCH_URL, _TOKEN)


class TestMacosGatewayLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.gw = MacosGateway("bot-bottle-gateway:latest")

    def test_is_running_reads_container_state(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            mod.container_is_running.return_value = True
            self.assertTrue(self.gw.is_running())
        with patch(f"{_GW}.container_mod") as mod:
            mod.container_is_running.return_value = False
            self.assertFalse(self.gw.is_running())

    def test_stop_removes_the_container(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            self.gw.stop()
        mod.force_remove_container.assert_called_once_with(self.gw.name)

    def test_address_reads_agent_network_ip(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = "192.168.128.3"
            self.assertEqual("192.168.128.3", self.gw.address())
        mod.try_container_ipv4_on_network.assert_called_once_with(
            self.gw.name, self.gw.network)

    def test_address_raises_when_unassigned(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = ""
            with self.assertRaises(GatewayError):
                self.gw.address()

    def test_ca_cert_pem_reads_from_the_gateway_container(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            mod.run_container_argv.return_value = _proc(stdout=_CA_PEM)
            self.assertEqual(_CA_PEM, self.gw.ca_cert_pem())
        argv = mod.run_container_argv.call_args.args[0]
        self.assertEqual(["container", "exec", self.gw.name, "cat"], argv[:4])

    def test_ca_cert_pem_raises_when_absent(self) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            mod.run_container_argv.return_value = _proc(returncode=1, stderr="no file")
            with self.assertRaises(GatewayError):
                self.gw.ca_cert_pem(timeout=0)

    def test_provisioning_transport_targets_the_gateway_container(self) -> None:
        from bot_bottle.backend.macos_container.gateway_transport import (
            MacosGatewayTransport,
        )
        transport = self.gw.provisioning_transport()
        assert isinstance(transport, MacosGatewayTransport)
        self.assertEqual(self.gw.name, transport.gateway)


if __name__ == "__main__":
    unittest.main()
