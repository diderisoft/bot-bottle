"""Unit: the macOS gateway data plane as an Apple container (PRD 0070)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend.macos_container.gateway import (
    GATEWAY_NAME,
    ORCHESTRATOR_AUTH_JWT_ENV,
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
        self.gw = MacosGateway("bot-bottle-gateway:latest", settings=Settings())

    def _connect(self, url: str = _ORCH_URL, token: str = _TOKEN) -> Mock:
        run = Mock(return_value=_proc())
        with patch(f"{_GW}.container_mod") as mod, \
                patch(f"{_GW}.host_gateway_ca_dir", return_value=Path("/ca")):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv = run
            mod.container_is_running.return_value = False
            self.assertTrue(self.gw.connect_to_orchestrator(url, token))
        return run

    def _connect_running(
        self, *, running_digest: str, env_url: str, env_token: str = _TOKEN,
    ) -> tuple[bool, Mock]:
        with patch(f"{_GW}.container_mod") as mod, \
                patch(f"{_GW}.host_gateway_ca_dir", return_value=Path("/ca")):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv = Mock(return_value=_proc())
            mod.container_is_running.return_value = True
            mod.container_image_digest.return_value = running_digest
            mod.image_digest.return_value = "sha256:current"
            mod.container_env.return_value = {
                "BOT_BOTTLE_ORCHESTRATOR_URL": env_url, ORCHESTRATOR_AUTH_JWT_ENV: env_token}
            restarted = self.gw.connect_to_orchestrator(_ORCH_URL, _TOKEN)
            return restarted, mod

    def test_adopts_a_running_current_gateway(self) -> None:
        # Recreating it on every launch wiped running bottles' git-gate state.
        restarted, mod = self._connect_running(
            running_digest="sha256:current", env_url=_ORCH_URL)
        self.assertFalse(restarted)
        mod.force_remove_container.assert_not_called()
        mod.run_container_argv.assert_not_called()

    def test_recreates_on_a_new_image_moved_orchestrator_or_new_token(self) -> None:
        # An unreadable digest ("") counts as not current: fail closed.
        for digest, url, token in (
            ("sha256:old", _ORCH_URL, _TOKEN),
            ("", _ORCH_URL, _TOKEN),
            ("sha256:current", "http://old:8099", _TOKEN),
            ("sha256:current", _ORCH_URL, "rotated-out"),
        ):
            with self.subTest(digest=digest, url=url, token=token):
                restarted, mod = self._connect_running(
                    running_digest=digest, env_url=url, env_token=token)
                self.assertTrue(restarted)
                mod.force_remove_container.assert_called_once()

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
        self.gw = MacosGateway("bot-bottle-gateway:latest", settings=Settings())

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

    def test_a_token_is_streamed_into_tmpfs_not_copied(self) -> None:
        # `container cp` lands beneath a tmpfs mount, where it's never seen.
        import tempfile  # pylint: disable=import-outside-toplevel
        from bot_bottle.backend.macos_container.gateway_transport import (
            MacosGatewayTransport,
        )
        transport = MacosGatewayTransport("gw")
        with tempfile.NamedTemporaryFile("w", suffix="-token", delete=False) as f:
            f.write("s3kr1t-value")
        self.addCleanup(os.unlink, f.name)
        ok = Mock(returncode=0, stderr="")
        with patch("bot_bottle.backend.macos_container.util.run_container_argv",
                   return_value=ok) as run:
            transport.cp_into(f.name, "/dev/shm/git-gate/b1/GH-token")
            transport.cp_into(f.name, "/etc/git-gate/pre-receive")
        streamed, copied = run.call_args_list
        self.assertEqual(["container", "exec", "-i", "gw", "sh", "-c", 'cat > "$1"', "sh",
                          "/dev/shm/git-gate/b1/GH-token"], streamed.args[0])
        self.assertEqual("s3kr1t-value", streamed.kwargs["stdin"])
        self.assertNotIn("s3kr1t", " ".join(streamed.args[0]))
        self.assertEqual(["container", "cp", f.name, "gw:/etc/git-gate/pre-receive"],
                         copied.args[0])


class TestMacosGatewayBuild(unittest.TestCase):
    def build(self, *, have_build: bool) -> tuple[MacosGateway, Mock]:
        gw = MacosGateway("bot-bottle-gateway:latest", settings=Settings())
        with patch(f"{_GW}.container_mod") as mod, \
                patch(f"{_GW}.pinned_infra_artifact", return_value=None), \
                patch(f"{_GW}.local_build_tag", return_value="bot-bottle-gateway:src-abc"):
            mod.image_exists.return_value = have_build
            gw.ensure_built()
        return gw, mod

    def test_builds_and_tags_when_this_code_has_no_build(self) -> None:
        gw, mod = self.build(have_build=False)
        mod.build_image.assert_called_once()
        mod.tag_image.assert_called_once_with("bot-bottle-gateway:latest",
                                              "bot-bottle-gateway:src-abc")
        self.assertEqual("bot-bottle-gateway:src-abc", gw.image_ref)

    def test_reuses_a_build_of_this_code(self) -> None:
        gw, mod = self.build(have_build=True)
        mod.build_image.assert_not_called()
        self.assertEqual("bot-bottle-gateway:src-abc", gw.image_ref)


if __name__ == "__main__":
    unittest.main()
