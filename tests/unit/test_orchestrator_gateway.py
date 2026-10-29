"""Unit tests for the consolidated Docker gateway (PRD 0070). Docker mocked."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.contract.hostctl import OperationSettings, Settings
from bot_bottle.backend.docker.gateway import (
    DEFAULT_GATEWAY_SUBNET,
    DockerGateway,
)
from bot_bottle.control.gateway import (
    GATEWAY_CA_CERT,
    GATEWAY_NAME,
    GatewayError,
    rotate_gateway_ca,
)
from bot_bottle.contract.identity import ORCHESTRATOR_AUTH_JWT_ENV
from bot_bottle.paths import GATEWAY_CA_DIRNAME, host_gateway_ca_dir
from tests.unit import use_bottle_root


_CA_PEM = "-----BEGIN CERTIFICATE-----\nMII...\n-----END CERTIFICATE-----\n"

_RUN_DOCKER = "bot_bottle.backend.docker.gateway.run_docker"
_DAEMON_RUNNING = "bot_bottle.backend.docker.gateway.daemon_running"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


_ORCH_URL = "http://orchestrator:9000"


def _healthy_network(argv: list[str]) -> Mock | None:
    """The answer for an `--internal`, correctly-labelled gateway network, or
    None when `argv` isn't a gateway-network inspect."""
    if argv[:3] != ["docker", "network", "inspect"] or "--format" not in argv:
        return None
    fmt = argv[argv.index("--format") + 1]
    if "Internal" in fmt:
        return _proc(stdout=f"{DEFAULT_GATEWAY_SUBNET}|true\n")
    return _proc(stdout="")  # attached containers: none
_TOKEN = "gw-jwt-token"


class TestDockerGateway(unittest.TestCase):
    def setUp(self) -> None:
        # Redirect the app-data root so ensure_running's host-dir mkdirs (CA +
        # DB) land in a throwaway dir, not the real ~/.bot-bottle.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name)))
        # Resolver-only data plane (PRD 0070): running the gateway requires an
        # orchestrator URL, so the fixture supplies one.
        self.sc = DockerGateway("bot-bottle-gateway:latest", settings=Settings())

    def test_default_name(self) -> None:
        self.assertEqual(GATEWAY_NAME, self.sc.name)

    def test_connect_refuses_without_orchestrator_url(self) -> None:
        # No policy source → the data-plane daemons would only crash-loop, so
        # the launch must fail closed with a clear error rather than start one.
        sc = DockerGateway("bot-bottle-gateway:latest", settings=Settings())
        with patch(_RUN_DOCKER) as m:
            with self.assertRaises(GatewayError):
                sc.connect_to_orchestrator("", _TOKEN)
        m.assert_not_called()

    def test_connect_refuses_without_gateway_token(self) -> None:
        # The gateway never mints — it must be handed a pre-minted `gateway`
        # token. Missing one is a misconfiguration, so fail closed before start.
        sc = DockerGateway("bot-bottle-gateway:latest", settings=Settings())
        with patch(_RUN_DOCKER) as m:
            with self.assertRaises(GatewayError):
                sc.connect_to_orchestrator(_ORCH_URL, "")
        m.assert_not_called()

    def test_is_running_reads_docker_ps(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(stdout=self.sc.name + "\n")):
            self.assertTrue(self.sc.is_running())
        with patch(_RUN_DOCKER, return_value=_proc(stdout="")):
            self.assertFalse(self.sc.is_running())

    def _running_gateway(self, calls: list[list[str]], *, token: str = _TOKEN,
                         url: str = _ORCH_URL, ip_forward: str = "0"):
        env = json.dumps([f"BOT_BOTTLE_ORCHESTRATOR_URL={url}",
                          f"{ORCHESTRATOR_AUTH_JWT_ENV}={token}"])

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if (net := _healthy_network(argv)) is not None:
                return net
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=self.sc.name)          # running
            if argv[:3] == ["docker", "image", "inspect"]:
                return _proc(stdout="img-A")               # current image id
            if argv[:2] == ["docker", "inspect"] and "IPAddress" in argv[3]:
                return _proc(stdout="10.242.255.2")        # at its pinned address
            if argv[:2] == ["docker", "inspect"] and "Sysctls" in argv[3]:
                return _proc(stdout=ip_forward)            # forwarding off
            if argv[:2] == ["docker", "inspect"] and "Config.Env" in argv[3]:
                return _proc(stdout=env)                   # its orchestrator binding
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout="img-A")               # container's image (same)
            return _proc()
        return fake

    def test_ensure_running_noop_when_up_and_image_current(self) -> None:
        calls: list[list[str]] = []
        with patch(_RUN_DOCKER, side_effect=self._running_gateway(calls)):
            restarted = self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertFalse(restarted)  # adopted: no running bottle needs reattaching
        self.assertEqual([], [c for c in calls if c[:2] == ["docker", "run"]])
        self.assertEqual([], [c for c in calls if c[:2] == ["docker", "rm"]])

    def test_a_gateway_bound_to_another_orchestrator_is_replaced(self) -> None:
        # Left by a run with another signing key, it would present a token the
        # current orchestrator rejects, so every request 403s.
        for kwargs in ({"token": "old-run-token"}, {"url": "http://elsewhere:9000"}):
            calls: list[list[str]] = []
            with patch(_RUN_DOCKER, side_effect=self._running_gateway(calls, **kwargs)):
                self.assertTrue(self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN), kwargs)
            self.assertEqual(1, len([c for c in calls if c[:2] == ["docker", "run"]]))

    def test_runs_with_ip_forwarding_off(self) -> None:
        # It is on every agent's network; it must not route between them.
        calls: list[list[str]] = []
        with patch(_RUN_DOCKER, side_effect=self._running_gateway(calls, token="old")):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        run = next(c for c in calls if c[:2] == ["docker", "run"])
        self.assertEqual("net.ipv4.ip_forward=0", run[run.index("--sysctl") + 1])

    def test_a_gateway_with_forwarding_on_is_replaced(self) -> None:
        for answer in ("", "1"):
            calls: list[list[str]] = []
            with patch(_RUN_DOCKER, side_effect=self._running_gateway(calls, ip_forward=answer)):
                self.assertTrue(self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN), answer)

    def test_an_unreadable_binding_counts_as_not_bound(self) -> None:
        for answer in (_proc(returncode=1, stderr="no such container"), _proc(stdout="{not json")):
            with patch(_RUN_DOCKER, return_value=answer):
                self.assertFalse(self.sc._bound_to(_ORCH_URL, _TOKEN))  # pyright: ignore[reportPrivateUsage]

    def test_gateway_off_its_pinned_address_is_recreated_there(self) -> None:
        # Agents bake the gateway's address into their proxy env, so a gateway
        # anywhere else would strand them: replace it at the pinned address.
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=self.sc.name)
            if argv[:3] == ["docker", "image", "inspect"]:
                return _proc(stdout="img-A")
            if argv[:2] == ["docker", "inspect"] and "IPAddress" in argv[3]:
                return _proc(stdout="10.242.255.9")
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout="img-A")
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.assertTrue(self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN))
        (run,) = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual("10.242.255.2", run[run.index("--ip") + 1])

    def test_ensure_running_recreates_when_image_is_stale(self) -> None:
        # Running, but the container was built from an OLD image → recreate so
        # a rebuild's new flat daemons take effect.
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=self.sc.name)
            if argv[:3] == ["docker", "image", "inspect"]:
                return _proc(stdout="img-NEW")
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout="img-OLD")
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            restarted = self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertTrue(restarted)
        self.assertEqual(1, len([c for c in calls if c[:2] == ["docker", "run"]]))
        self.assertTrue(any(c[:2] == ["docker", "rm"] for c in calls))

    def test_ensure_running_starts_the_singleton_when_absent(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)

        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        self.assertIn(self.sc.name, runs[0])
        self.assertIn("bot-bottle-gateway:latest", runs[0])
        # Runs on the shared gateway network so agents can reach it by IP.
        self.assertEqual(self.sc.network, runs[0][runs[0].index("--network") + 1])
        # Persists its CA on a HOST bind-mount (not a docker named volume, which
        # `docker volume prune` would wipe — issue #450) so agents keep trusting
        # it across restarts. The mount source is the host gateway-CA dir.
        ca_mounts = [
            a for a in runs[0]
            if a.endswith(":/home/mitmproxy/.mitmproxy")
        ]
        self.assertEqual(1, len(ca_mounts))
        src = ca_mounts[0].rsplit(":", 1)[0]
        self.assertTrue(src.endswith("/" + GATEWAY_CA_DIRNAME), src)
        self.assertTrue(Path(src).is_absolute(), src)
        # No DB handle on the data plane: the supervise queue is reached over
        # the control-plane RPC, so the gateway container carries neither the
        # DB bind-mount nor SUPERVISE_DB_PATH (PRD 0070 / issue #469).
        self.assertFalse(any(a.startswith("SUPERVISE_DB_PATH=") for a in runs[0]))
        self.assertFalse(any(a.endswith(":/run/supervise") for a in runs[0]))
        # Data plane resolves policy against the orchestrator control plane.
        self.assertIn(f"BOT_BOTTLE_ORCHESTRATOR_URL={_ORCH_URL}", runs[0])

    def test_named_ca_volume_supports_socket_shared_runner(self) -> None:
        sc = DockerGateway(
            "bot-bottle-gateway:latest", ca_mount_source="ci-ca-volume", settings=Settings()
        )

        def fake(argv: list[str], **_kw: object) -> Mock:
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake) as run:
            sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        argv = next(c.args[0] for c in run.call_args_list
                    if c.args[0][:2] == ["docker", "run"])
        self.assertIn(
            "ci-ca-volume:/home/mitmproxy/.mitmproxy", argv
        )

    def test_connect_injects_the_pre_minted_gateway_token(self) -> None:
        # The gateway presents the token the orchestrator handed it — it never
        # mints (holds no signing key). The value rides the env (bare `--env
        # NAME` on argv keeps it off `docker inspect`), not argv itself.

        run_env: dict[str, str] = {}

        def fake(argv: list[str], **kw: object) -> Mock:
            if argv[:2] == ["docker", "run"]:
                env = kw.get("env")
                if isinstance(env, dict):
                    run_env.update(env)
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertEqual(_TOKEN, run_env[ORCHESTRATOR_AUTH_JWT_ENV])

    def test_address_reads_container_ip_on_the_agent_network(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(stdout="172.18.0.2\n")) as m:
            self.assertEqual("172.18.0.2", self.sc.address())
        argv = m.call_args.args[0]
        self.assertEqual(["docker", "inspect"], argv[:2])
        self.assertIn(self.sc.name, argv)
        self.assertTrue(any(self.sc.network in a for a in argv))

    def test_address_raises_when_unassigned(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(stdout="")):
            with self.assertRaises(GatewayError):
                self.sc.address()

    def test_provisioning_transport_targets_the_gateway_container(self) -> None:
        from bot_bottle.backend.docker.gateway_transport import DockerGatewayTransport

        transport = self.sc.provisioning_transport()
        assert isinstance(transport, DockerGatewayTransport)
        self.assertEqual(self.sc.name, transport.gateway)

    def test_ensure_running_creates_network_when_missing(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(returncode=1, stderr="No such network")
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        creates = [c for c in calls if c[:3] == ["docker", "network", "create"]
                   and c[-1] == self.sc.network]
        self.assertEqual(
            [[
                "docker", "network", "create",
                # IPv6 is disabled so a default-IPv6 daemon can't attach a
                # malformed fdd0::/64 subnet that breaks `network inspect`.
                "--ipv6=false",
                # No route off the box: agents reach only the gateway.
                "--internal",
                "--subnet", DEFAULT_GATEWAY_SUBNET,
                "--label",
                f"bot-bottle.gateway-subnet={DEFAULT_GATEWAY_SUBNET}",
                self.sc.network,
            ]],
            creates,
        )

    def test_ensure_running_replaces_stale_auto_ipam_network(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(stdout="<no value>|true\n")
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertIn(
            ["docker", "rm", "--force", self.sc.name],
            calls,
        )
        self.assertIn(
            ["docker", "network", "rm", self.sc.network],
            calls,
        )

    def test_ensure_running_replaces_poisoned_ipv6_network(self) -> None:
        # A daemon that default-enables IPv6 leaves the gateway network with a
        # malformed fdd0::/64 gateway, so `docker network inspect` exits
        # non-zero with a ParseAddr error (not "No such network"). `--ipv6=false`
        # can't heal an already-poisoned network — the create just no-ops on
        # "already exists" — so _ensure_network must force-remove and recreate
        # it, else every later subnet read keeps failing.
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(
                    returncode=1,
                    stderr='ParseAddr("fdd0:0:0:4::1/64"): unexpected character, '
                    'want colon (at "/64")',
                )
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertIn(["docker", "rm", "--force", self.sc.name], calls)
        self.assertIn(["docker", "network", "rm", self.sc.network], calls)
        creates = [c for c in calls if c[:3] == ["docker", "network", "create"]
                   and c[-1] == self.sc.network]
        self.assertEqual(
            [[
                "docker", "network", "create",
                "--ipv6=false",
                "--internal",
                "--subnet", DEFAULT_GATEWAY_SUBNET,
                "--label",
                f"bot-bottle.gateway-subnet={DEFAULT_GATEWAY_SUBNET}",
                self.sc.network,
            ]],
            creates,
        )

    def test_ensure_running_creates_network_when_inspect_reports_absent(self) -> None:
        # The absent case (inspect fails with "No such network") must NOT try to
        # remove anything — it just creates. Guards the poisoned-vs-absent split.
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(returncode=1, stderr="Error: No such network: x")
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertNotIn(["docker", "network", "rm", self.sc.network], calls)
        creates = [c for c in calls if c[:3] == ["docker", "network", "create"]
                   and c[-1] == self.sc.network]
        self.assertEqual(1, len(creates))

    def test_ensure_running_does_not_destroy_on_generic_inspect_error(self) -> None:
        # A generic inspect failure (daemon hiccup, permission, timeout) is NOT
        # evidence of a poisoned network. Only the ParseAddr poison signature may
        # take the destructive heal path; anything else must surface as an error
        # without tearing down a possibly-healthy shared gateway.
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(
                    returncode=1,
                    stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
                )
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            with self.assertRaises(GatewayError):
                self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        # No mutation of the shared gateway: neither the container nor the
        # network is removed, and nothing is recreated.
        self.assertNotIn(["docker", "network", "rm", self.sc.network], calls)
        self.assertFalse(any(c[:3] == ["docker", "rm", "--force"] for c in calls))
        self.assertEqual([], [c for c in calls if c[:3] == ["docker", "network", "create"]])

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
                patch("bot_bottle.backend.docker.gateway.time.sleep"):
            self.assertEqual(_CA_PEM, self.sc.ca_cert_pem(timeout=5))

    def test_ensure_running_reuses_existing_network(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if (net := _healthy_network(argv)) is not None:
                return net
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)  # network inspect returns 0 → exists
        self.assertEqual([], [c for c in calls if c[:3] == ["docker", "network", "create"]])
        self.assertNotIn(["docker", "network", "rm", self.sc.network], calls)

    def test_gateway_joins_the_egress_network_agents_are_not_on(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if (net := _healthy_network(argv)) is not None:
                return net
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(returncode=1, stderr="No such network")
            return _proc(stdout="") if argv[:2] == ["docker", "ps"] else _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertNotEqual(self.sc.network, self.sc.egress_network)
        self.assertIn(
            ["docker", "network", "create", "--ipv6=false", self.sc.egress_network], calls)
        self.assertIn(
            ["docker", "network", "connect", self.sc.egress_network, self.sc.name], calls)

    def _non_internal_network(self, calls: list[list[str]], attached: str) -> object:
        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:3] == ["docker", "network", "inspect"] and "--format" in argv:
                fmt = argv[argv.index("--format") + 1]
                if "Internal" in fmt:
                    return _proc(stdout=f"{DEFAULT_GATEWAY_SUBNET}|false\n")
                return _proc(stdout=attached)
            return _proc()
        return fake

    def test_a_non_internal_network_is_replaced_when_nothing_is_attached(self) -> None:
        # Older releases created the agent network without `--internal`, which
        # gives agents a route that bypasses the gateway. It must be recreated.
        calls: list[list[str]] = []
        with patch(_RUN_DOCKER, side_effect=self._non_internal_network(
                calls, f"{self.sc.name} ")):
            self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertIn(["docker", "network", "rm", self.sc.network], calls)
        creates = [c for c in calls if c[:3] == ["docker", "network", "create"]
                   and c[-1] == self.sc.network]
        self.assertEqual(1, len(creates))
        self.assertIn("--internal", creates[0])

    def test_a_non_internal_network_with_bottles_attached_fails_closed(self) -> None:
        # Replacing it would strand the running bottles, and launching onto it
        # would leak: refuse, and change nothing.
        calls: list[list[str]] = []
        with patch(_RUN_DOCKER, side_effect=self._non_internal_network(
                calls, f"{self.sc.name} bot-bottle-demo-abc ")):
            with self.assertRaises(GatewayError) as cm:
                self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        self.assertIn("bot-bottle-demo-abc", str(cm.exception))
        self.assertNotIn(["docker", "network", "rm", self.sc.network], calls)
        self.assertFalse(any(c[:3] == ["docker", "rm", "--force"] for c in calls))
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in calls))

    def test_ensure_running_raises_on_docker_failure(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:2] == ["docker", "run"]:
                return _proc(returncode=1, stderr="boom")
            return _proc()

        with patch(_RUN_DOCKER, side_effect=fake):
            with self.assertRaises(GatewayError):
                self.sc.connect_to_orchestrator(_ORCH_URL, _TOKEN)

    def test_stop_is_idempotent_on_missing(self) -> None:
        absent = _proc(returncode=1, stderr="Error: No such container: x")
        with patch(_RUN_DOCKER, return_value=absent):
            self.sc.stop()  # must not raise

    def test_stop_raises_on_other_failure(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1, stderr="permission denied")), \
                patch(_DAEMON_RUNNING, return_value=True):
            with self.assertRaises(GatewayError):
                self.sc.stop()

    def test_stop_on_a_stopped_daemon_is_already_stopped(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1, stderr="any wording")), \
                patch(_DAEMON_RUNNING, return_value=False):
            self.sc.stop()  # must not raise


class TestDockerGatewayBuild(unittest.TestCase):
    def setUp(self) -> None:
        self.sc = DockerGateway(settings=Settings())  # defaults to the real bundle image + dockerfile

    def test_image_exists_reads_docker_inspect(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=0)):
            self.assertTrue(self.sc.image_exists())
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1)):
            self.assertFalse(self.sc.image_exists())

    def test_ensure_built_builds_even_when_image_present(self) -> None:
        # Always build (cache-aware) so a flat-source change rebuilds; the old
        # build-if-missing silently ran a stale single-tenant image.
        calls: list[list[str]] = []

        def rec(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            return _proc()  # image present, build succeeds

        with patch(_RUN_DOCKER, side_effect=rec):
            self.sc.ensure_built()
        builds = [c for c in calls if c[:2] == ["docker", "build"]]
        self.assertEqual(1, len(builds))
        self.assertIn(self.sc.image_ref, builds[0])
        self.assertTrue(any(a.endswith("Dockerfile.gateway") for a in builds[0]))
        arg_index = builds[0].index("--build-arg")
        self.assertTrue(
            builds[0][arg_index + 1].startswith("PYTHON_BASE_IMAGE=python:"),
        )
        self.assertNotIn("--no-cache", builds[0])

    def test_ensure_built_no_cache_forces_full_rebuild(self) -> None:
        calls: list[list[str]] = []

        def rec(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            return _proc()

        no_cache = Settings(operation=OperationSettings(no_cache=True))
        with patch(_RUN_DOCKER, side_effect=rec):
            DockerGateway(settings=no_cache).ensure_built()
        builds = [c for c in calls if c[:2] == ["docker", "build"]]
        self.assertIn("--no-cache", builds[0])

    def test_ensure_built_noop_when_no_dockerfile(self) -> None:
        sc = DockerGateway("busybox", dockerfile=None, settings=Settings())
        with patch(_RUN_DOCKER) as m:
            sc.ensure_built()
        m.assert_not_called()

    def test_ensure_built_raises_on_build_failure(self) -> None:
        with patch(_RUN_DOCKER, return_value=_proc(returncode=1, stderr="build boom")):
            with self.assertRaises(GatewayError):
                self.sc.ensure_built()


class TestRotateGatewayCa(unittest.TestCase):
    """rotate_gateway_ca clears the persisted CA so the next start remints it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name)))

    def _seed_ca(self) -> Path:
        ca_dir = host_gateway_ca_dir()
        # A representative mitmproxy confdir: the CA identity + derived encodings,
        # plus one non-CA file that rotation must leave untouched.
        for name in (
            "mitmproxy-ca.pem",
            "mitmproxy-ca-cert.pem",
            "mitmproxy-ca-cert.cer",
            "mitmproxy-ca-cert.p12",
        ):
            (ca_dir / name).write_text("x")
        (ca_dir / "combined-trust.pem").write_text("keep")
        return ca_dir

    def test_removes_ca_material_only(self) -> None:
        ca_dir = self._seed_ca()
        removed = rotate_gateway_ca(ca_dir)
        self.assertEqual(4, len(removed))
        self.assertTrue(all(p.name.startswith("mitmproxy-ca") for p in removed))
        # The CA files are gone; the non-CA trust bundle survives.
        self.assertEqual(
            {"combined-trust.pem"}, {p.name for p in ca_dir.iterdir()}
        )

    def test_defaults_to_host_ca_dir(self) -> None:
        self._seed_ca()
        removed = rotate_gateway_ca()  # no arg → host_gateway_ca_dir()
        self.assertTrue(removed)
        self.assertEqual(
            [], list(host_gateway_ca_dir().glob("mitmproxy-ca*"))
        )

    def test_idempotent_when_no_ca(self) -> None:
        self.assertEqual([], rotate_gateway_ca(host_gateway_ca_dir()))


if __name__ == "__main__":
    unittest.main()
