"""Unit: `GET /bottles/live` (PRD 0092 chunk 7): the bottles running on each
backend, the route's roles and backend scoping, and the orchestrator's
`HostControlRelay.live_source_ips` reading it."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from bot_bottle.backend.gateway_attach import GatewayAttachError, RunningBottle
from bot_bottle.contract.auth import ROLE_CLI, ROLE_GATEWAY
from bot_bottle.contract.hostctl import (
    HOST_CONTROL, LIVE_BOTTLES_PATH, ROLE_ORCHESTRATOR, InfraSettings, bearer,
)
from bot_bottle.control.hostctl import live as live_mod
from bot_bottle.control.hostctl.__main__ import GUEST_ROLES
from bot_bottle.control.hostctl.live import LiveBottles
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.lib.log import Die
from bot_bottle.orchestrator.hostctl_relay import HostControlRelay, LiveBottlesUnknown

_KEY = "h" * 32
_NAMES = ("docker", "firecracker", "macos-container")


def _live(running: dict[str, list[RunningBottle] | BaseException], *,
          available: tuple[str, ...] = _NAMES,
          recorded: dict[str, InfraSettings] | None = None) -> LiveBottles:
    def run(name: str, infra: InfraSettings | None) -> list[RunningBottle]:
        del infra
        result = running.get(name, [])
        if isinstance(result, BaseException):
            raise result
        return result
    return LiveBottles(names=lambda: _NAMES, available=lambda n: n in available,
                       running=run, recorded=lambda n: (recorded or {}).get(n))


def _bottle(slug: str, ip: str) -> RunningBottle:
    return RunningBottle(ref=f"bot-bottle-{slug}", slug=slug, source_ip=ip)


class TestLiveBottles(unittest.TestCase):
    def test_lists_each_available_backend(self) -> None:
        live = _live({"docker": [_bottle("a", "172.30.0.5")],
                      "macos-container": [_bottle("b", "192.168.64.7"), _bottle("c", "")]},
                     available=("docker", "macos-container"))
        self.assertEqual({
            "bottles": [
                {"backend": "docker", "slug": "a", "source_ip": "172.30.0.5"},
                {"backend": "macos-container", "slug": "b", "source_ip": "192.168.64.7"},
                # No address yet (mid-launch): listed, with an empty source IP.
                {"backend": "macos-container", "slug": "c", "source_ip": ""},
            ],
            "errors": {},
        }, live.list())

    def test_a_failed_backend_is_an_error_not_an_empty_list(self) -> None:
        live = _live({"docker": GatewayAttachError("docker ps failed"),
                      "firecracker": Die(1, "no kvm"),
                      "macos-container": [_bottle("b", "192.168.64.7")]})
        got = live.list()
        self.assertEqual({"docker": "docker ps failed", "firecracker": "no kvm"}, got["errors"])
        self.assertEqual(["b"], [b["slug"] for b in got["bottles"]])  # type: ignore[index]

    def test_narrows_to_one_backend(self) -> None:
        live = _live({"docker": GatewayAttachError("docker ps failed"),
                      "macos-container": [_bottle("b", "192.168.64.7")]})
        # Another backend's failure doesn't spoil this one's answer.
        self.assertEqual({"bottles": [{"backend": "macos-container", "slug": "b",
                                       "source_ip": "192.168.64.7"}], "errors": {}},
                         live.list("macos-container"))
        self.assertTrue(live.known("docker"))
        self.assertFalse(live.known("podman"))

    def test_runs_on_the_recorded_infra_settings(self) -> None:
        infra = InfraSettings(egress_port=9999)
        seen: dict[str, InfraSettings | None] = {}

        def run(name: str, got: InfraSettings | None) -> list[RunningBottle]:
            seen[name] = got
            return []
        LiveBottles(names=lambda: ("docker", "firecracker"), available=lambda _n: True,
                    running=run, recorded={"docker": infra}.get).list()
        self.assertEqual({"docker": infra, "firecracker": None}, seen)

    def test_reuses_the_backends_gateway_attacher(self) -> None:
        backend = MagicMock()
        backend.infra_service.return_value.gateway_attacher.return_value \
            .running_bottles.return_value = [_bottle("a", "10.243.0.2")]
        with patch("bot_bottle.backend.get_bottle_backend", return_value=backend) as get:
            got = live_mod._running("firecracker", None)
        self.assertEqual([_bottle("a", "10.243.0.2")], got)
        self.assertEqual("firecracker", get.call_args.args[1])
        self.assertFalse(get.call_args.kwargs["prompt"])


def _get(url: str, role: str | None) -> tuple[int, dict[str, object]]:
    headers = bearer(HOST_CONTROL.mint(role, _KEY)) if role else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class _Served(unittest.TestCase):
    def serve(self, live: LiveBottles, **kwargs: object) -> str:
        server = HostControlServer(("127.0.0.1", 0), "b", _KEY, live=live,
                                   **kwargs)  # type: ignore[arg-type]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"


class TestRoute(_Served):
    def setUp(self) -> None:
        self.live = _live({"docker": [_bottle("a", "172.30.0.5")],
                           "macos-container": [_bottle("b", "192.168.64.7")]})

    def test_roles(self) -> None:
        url = self.serve(self.live) + LIVE_BOTTLES_PATH
        for role in (ROLE_CLI, ROLE_ORCHESTRATOR):
            status, body = _get(url, role)
            self.assertEqual(200, status, role)
            self.assertEqual(["a", "b"], [b["slug"] for b in body["bottles"]])  # type: ignore[union-attr]
        self.assertEqual(403, _get(url, ROLE_GATEWAY)[0])
        self.assertEqual(401, _get(url, None)[0])

    def test_backend_query_narrows_the_cli_view(self) -> None:
        url = self.serve(self.live) + LIVE_BOTTLES_PATH
        status, body = _get(url + "?backend=docker", ROLE_CLI)
        self.assertEqual((200, ["a"]), (status, [b["slug"] for b in body["bottles"]]))  # type: ignore[union-attr]
        status, body = _get(url + "?backend=podman", ROLE_CLI)
        self.assertEqual(400, status)
        self.assertIn("podman", str(body["error"]))

    def test_a_guest_listener_answers_for_its_own_backend_only(self) -> None:
        url = self.serve(self.live, peers="127.0.0.1/32", roles=GUEST_ROLES,
                         backend="macos-container") + LIVE_BOTTLES_PATH
        for query in ("", "?backend=docker"):
            status, body = _get(url + query, ROLE_ORCHESTRATOR)
            self.assertEqual((200, ["b"]), (status, [b["slug"] for b in body["bottles"]]))  # type: ignore[union-attr]
        self.assertEqual(401, _get(url, ROLE_CLI)[0])


class TestRelay(_Served):
    def relay(self, live: LiveBottles, **kwargs: object) -> HostControlRelay:
        url = self.serve(live, peers="127.0.0.1/32", roles=GUEST_ROLES, **kwargs)
        return HostControlRelay(url, HOST_CONTROL.mint(ROLE_ORCHESTRATOR, _KEY))

    def test_reads_the_source_ips_of_its_backend(self) -> None:
        relay = self.relay(_live({"docker": GatewayAttachError("docker ps failed"),
                                  "firecracker": [_bottle("a", "10.243.0.2"),
                                                  _bottle("b", "")]}),
                           backend="firecracker")
        self.assertEqual(["10.243.0.2"], relay.live_source_ips())

    def test_a_backend_it_couldnt_list_is_unknown(self) -> None:
        relay = self.relay(_live({"firecracker": GatewayAttachError("ssh key missing")}),
                           backend="firecracker")
        with self.assertRaisesRegex(LiveBottlesUnknown, "ssh key missing"):
            relay.live_source_ips()

    def test_a_refusal_is_unknown(self) -> None:
        url = self.serve(_live({}))
        relay = HostControlRelay(url, HOST_CONTROL.mint(ROLE_GATEWAY, _KEY))
        with self.assertRaisesRegex(LiveBottlesUnknown, "403"):
            relay.live_source_ips()

    def test_an_unreachable_daemon_is_unknown(self) -> None:
        url = self.serve(_live({}))
        relay = HostControlRelay(url.rsplit(":", 1)[0] + ":1", "t")
        with self.assertRaisesRegex(LiveBottlesUnknown, "unreachable"):
            relay.live_source_ips()


if __name__ == "__main__":
    unittest.main()
