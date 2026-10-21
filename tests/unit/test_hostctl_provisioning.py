"""Unit: the host controller reachable from the orchestrator (PRD 0092 chunk 6):
the URL + `orchestrator` token each backend's launcher hands the orchestrator,
the host address each backend listens on, and the guest listeners' peer and
role limits."""

from __future__ import annotations

import errno
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.docker.orchestrator import DockerOrchestrator
from bot_bottle.backend.firecracker import infra_vm
from bot_bottle.backend.firecracker.orchestrator import FirecrackerOrchestrator
from bot_bottle.backend.macos_container import util as mac_util
from bot_bottle.backend.macos_container.orchestrator import MacosOrchestrator
from bot_bottle.contract.auth import ROLE_CLI
from bot_bottle.contract.hostctl import (
    BOTTLES_PATH, GUEST_PORT, HOST_CONTROL, HOSTCTL_URL_ENV, ROLE_ORCHESTRATOR, Settings,
    bearer,
)
from bot_bottle.control.hostctl.__main__ import GUEST_ROLES
from bot_bottle.control.hostctl.listeners import GuestListeners
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.control.lifecycle import Orchestrator
from bot_bottle.trust_domain import HostControlProvisioning, HostLink

_KEY = "h" * 32
_TOKEN_FILE = "bot_bottle.trust_domain.host_signing_key"


def _key_file(filename: str) -> str:
    """The host key files, without touching the real ones."""
    return _KEY if filename == HOST_CONTROL.key_filename else "cp-key"


class _Provisioned(unittest.TestCase):
    """Runs each test as if inside the host controller."""

    def setUp(self) -> None:
        self.listened: list[HostLink] = []
        self.enterContext(patch.object(
            Orchestrator, "host_control", HostControlProvisioning(listen=self.listened.append)))
        self.enterContext(patch(_TOKEN_FILE, side_effect=_key_file))

    def assert_provisioned(self, env: dict[str, str], address: str) -> None:
        self.assertEqual(f"http://{address}:{GUEST_PORT}", env[HOSTCTL_URL_ENV])
        self.assertEqual(ROLE_ORCHESTRATOR, HOST_CONTROL.verify(env[HOST_CONTROL.token_env], _KEY))
        # The orchestrator never gets the host controller's key.
        self.assertNotIn(HOST_CONTROL.key_env, env)
        self.assertNotIn(_KEY, env.values())


class TestProvisioning(_Provisioned):
    def test_env_carries_the_url_and_an_orchestrator_token_and_listens(self) -> None:
        link = HostLink("10.243.255.0", "10.243.255.1/32")
        env = Orchestrator.host_control.orchestrator_env(link)  # type: ignore[union-attr]
        self.assert_provisioned(env, "10.243.255.0")
        self.assertEqual([link], self.listened)

    def test_nothing_outside_the_host_controller(self) -> None:
        with patch.object(Orchestrator, "host_control", None):
            orch = FirecrackerOrchestrator(Settings())
            self.assertEqual({}, orch.host_control_env())
            orch.serve_host_control()
        self.assertEqual([], self.listened)


class TestDocker(_Provisioned):
    def _fake(self, inspect: str):  # type: ignore[no-untyped-def]
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:3] == ["docker", "network", "inspect"] and "{{.Gateway}}" in " ".join(argv):
                return Mock(returncode=0, stdout=inspect, stderr="")
            if argv[:3] == ["docker", "network", "inspect"]:
                return Mock(returncode=0, stdout="false", stderr="")  # masquerade off
            return Mock(returncode=0, stdout="", stderr="")
        return fake

    def test_host_link_is_the_publish_bridge_skipping_ipv6(self) -> None:
        orch = DockerOrchestrator(settings=Settings(), repo_root=Path("/r"))
        with patch("bot_bottle.backend.docker.orchestrator.run_docker",
                   side_effect=self._fake("fd00::1 fd00::/64 172.30.0.1 172.30.0.0/16 ")):
            self.assertEqual(HostLink("172.30.0.1", "172.30.0.0/16"), orch.host_link())
        with patch("bot_bottle.backend.docker.orchestrator.run_docker",
                   return_value=Mock(returncode=1, stdout="", stderr="no such network")):
            self.assertIsNone(orch.host_link())

    def test_run_passes_url_and_token_by_name(self) -> None:
        orch = DockerOrchestrator(settings=Settings(), repo_root=Path("/r"))
        with patch("bot_bottle.backend.docker.orchestrator.run_docker",
                   side_effect=self._fake("172.30.0.1 172.30.0.0/16")) as run:
            orch._run_container("h")
        call = next(c for c in run.call_args_list if c.args[0][:2] == ["docker", "run"])
        argv, env = call.args[0], call.kwargs["env"]
        for name in (HOSTCTL_URL_ENV, HOST_CONTROL.token_env):
            self.assertEqual("--env", argv[argv.index(name) - 1])  # by name: value off argv
        self.assert_provisioned(env, "172.30.0.1")
        self.assertNotIn(env[HOST_CONTROL.token_env], argv)
        self.assertEqual([HostLink("172.30.0.1", "172.30.0.0/16")], self.listened)


class TestMacos(_Provisioned):
    def test_network_ipv4_reads_gateway_and_subnet(self) -> None:
        body = json.dumps([{"status": {"ipv4Gateway": "192.168.129.1",
                                       "ipv4Subnet": "192.168.129.0/24"}}])
        with patch.object(mac_util, "run_container_argv",
                          return_value=Mock(returncode=0, stdout=body)):
            self.assertEqual(("192.168.129.1", "192.168.129.0/24"),
                             mac_util.network_ipv4("bot-bottle-mac-control"))
        bad = json.dumps([{"status": {"ipv4Gateway": "10.0.0.1", "ipv4Subnet": "192.168.0.0/24"}}])
        with patch.object(mac_util, "run_container_argv",
                          return_value=Mock(returncode=0, stdout=bad)):
            self.assertIsNone(mac_util.network_ipv4("x"))

    def test_run_passes_url_and_token_by_name(self) -> None:
        run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
        with patch("bot_bottle.backend.macos_container.orchestrator.container_mod") as mod:
            mod.network_ipv4.return_value = ("192.168.129.1", "192.168.129.0/24")
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.return_value = "spec"
            mod.run_container_argv = run
            orch = MacosOrchestrator(repo_root=Path("/r"), settings=Settings())
            orch._run_container("h")
            mod.network_ipv4.assert_called_with("bot-bottle-mac-control")
        argv, env = run.call_args.args[0], run.call_args.kwargs["env"]
        for name in (HOSTCTL_URL_ENV, HOST_CONTROL.token_env):
            self.assertEqual("--env", argv[argv.index(name) - 1])
        self.assert_provisioned(env, "192.168.129.1")
        self.assertEqual([HostLink("192.168.129.1", "192.168.129.0/24")], self.listened)


class TestFirecracker(_Provisioned):
    def test_host_link_is_the_orchestrator_taps_host_end(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        slot = infra_vm.netpool.from_settings(Settings().infra).orch_slot()
        self.assertEqual(HostLink(slot.host_ip, f"{slot.guest_ip}/32"), orch.host_link())
        self.assertEqual("10.243.255.0", slot.host_ip)

    def test_pushes_url_and_token_after_the_key(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        vm = infra_vm.InfraVm(guest_ip="10.243.255.1", private_key=Path("/k"), vm=MagicMock())
        with patch.object(infra_vm, "boot_vm", return_value=vm), \
                patch.object(infra_vm, "push_secret") as push, \
                patch.object(orch, "is_running", return_value=False), \
                patch.object(orch, "_ensure_registry_volume", return_value=Path("/reg")), \
                patch.object(orch, "_ensure_build_store_volume", return_value=Path("/bs")), \
                patch.object(orch, "_wait_for_health"):
            orch.ensure_running()
        dests = [c.args[2] for c in push.call_args_list]
        self.assertEqual([infra_vm._GUEST_SIGNING_KEY_PATH, infra_vm.GUEST_HOSTCTL_PATH], dests)
        url, token = push.call_args.args[1].splitlines()
        self.assert_provisioned({HOSTCTL_URL_ENV: url, HOST_CONTROL.token_env: token},
                                "10.243.255.0")

    def test_nixos_opens_the_port_on_the_orchestrator_tap_only(self) -> None:
        nix = (Path(__file__).resolve().parents[2] / "nix" / "firecracker-netpool.nix").read_text()
        self.assertIn("networking.firewall.interfaces.${cfg.orchIface}.allowedTCPPorts = "
                      f"[ {GUEST_PORT} ];", nix)
        self.assertEqual(1, nix.count("allowedTCPPorts"))

    def test_init_starts_the_control_plane_with_them(self) -> None:
        init = infra_vm._orchestrator_init()
        self.assertIn(f"[ ! -f {infra_vm.GUEST_HOSTCTL_PATH} ]", init)
        self.assertIn(f'{HOSTCTL_URL_ENV}="$HC_URL" {HOST_CONTROL.token_env}="$HC_JWT"', init)


def _call(url: str, role: str | None) -> int:
    headers = bearer(HOST_CONTROL.mint(role, _KEY)) if role else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class TestGuestServer(unittest.TestCase):
    def serve(self, peers: str) -> str:
        server = HostControlServer(("127.0.0.1", 0), "b", _KEY, peers=peers, roles=GUEST_ROLES)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_accepts_the_orchestrator_role_only(self) -> None:
        url = self.serve("127.0.0.1/32") + BOTTLES_PATH
        self.assertEqual(200, _call(url, ROLE_ORCHESTRATOR))
        # Only the host holds `cli`; a guest listener doesn't take it.
        self.assertEqual(401, _call(url, ROLE_CLI))

    def test_drops_peers_outside_its_network(self) -> None:
        url = self.serve("10.243.255.1/32") + BOTTLES_PATH
        with self.assertRaises((urllib.error.URLError, ConnectionError)):
            _call(url, ROLE_ORCHESTRATOR)


class TestGuestListeners(unittest.TestCase):
    def setUp(self) -> None:
        self.made: list[HostLink] = []

        def make(link: HostLink) -> HostControlServer:
            self.made.append(link)
            return HostControlServer((link.address, 0), "b", _KEY, peers=link.network,
                                     roles=GUEST_ROLES)

        self.listeners = GuestListeners(make, retry_seconds=2)
        self.addCleanup(self.listeners.close)

    def test_listen_is_idempotent_and_rebinds_for_a_new_network(self) -> None:
        self.listeners.listen(HostLink("127.0.0.1", "127.0.0.1/32"))
        self.listeners.listen(HostLink("127.0.0.1", "127.0.0.1/32"))
        self.assertEqual(["127.0.0.1"], self.listeners.addresses())
        self.assertEqual(1, len(self.made))
        self.listeners.listen(HostLink("127.0.0.1", "127.0.0.0/8"))
        self.assertEqual(2, len(self.made))

    def test_an_address_not_up_yet_is_retried(self) -> None:
        attempts: list[int] = []
        real = self.listeners._make_server

        def flaky(link: HostLink) -> HostControlServer:
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError(errno.EADDRNOTAVAIL, "not up")
            return real(link)

        self.listeners._make_server = flaky
        done = threading.Event()
        with patch("bot_bottle.control.hostctl.listeners._RETRY_POLL_SECONDS", 0.01):
            self.listeners.listen(HostLink("127.0.0.1", "127.0.0.1/32"))
            for _ in range(200):
                if self.listeners.addresses():
                    done.set()
                    break
                threading.Event().wait(0.01)
        self.assertTrue(done.is_set())
        self.assertEqual(3, len(attempts))

    def test_other_bind_failures_are_logged_not_raised(self) -> None:
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        self.addCleanup(taken.close)
        port = taken.getsockname()[1]
        listeners = GuestListeners(lambda link: HostControlServer(
            (link.address, port), "b", _KEY, peers=link.network))
        with patch("bot_bottle.control.hostctl.listeners.warn") as warn:
            listeners.listen(HostLink("127.0.0.1", "127.0.0.1/32"))
        warn.assert_called_once()
        self.assertEqual([], listeners.addresses())


if __name__ == "__main__":
    unittest.main()
