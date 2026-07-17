"""Integration: the Docker orchestrator's control plane enforces the
per-host auth secret against a real container (issue #400).

Unit tests exercise `dispatch()` in-process, socket-free. This starts the
actual orchestrator + gateway as Docker containers and drives the real HTTP
server over its published loopback port, the same path an agent sharing the
gateway network — or the trusted host CLI — would use.

Gated on a reachable Docker daemon. Uses unique container/network names and
a throwaway `BOT_BOTTLE_ROOT` so it never collides with a real per-host
orchestrator or gateway.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from bot_bottle.orchestrator.control_plane import CONTROL_AUTH_HEADER
from bot_bottle.orchestrator.gateway import DockerGateway
from bot_bottle.orchestrator.lifecycle import OrchestratorService
from bot_bottle.paths import host_control_plane_token
from tests._docker import skip_unless_docker


class _IsolatedOrchestratorService(OrchestratorService):
    """`OrchestratorService`, but with a uniquely-named gateway too (the base
    class only parameterizes the orchestrator's own name/network/port) so a
    test run can never touch a real host's orchestrator or gateway."""

    def __init__(self, *, gateway_name: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._test_gateway_name = gateway_name

    def _gateway(self) -> DockerGateway:
        return DockerGateway(
            self._gateway_image,
            name=self._test_gateway_name,
            network=self.network,
            orchestrator_url=self.internal_url,
        )


@skip_unless_docker()
class TestDockerControlPlaneAuthIntegration(unittest.TestCase):
    def setUp(self) -> None:
        suffix = secrets.token_hex(4)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.svc = _IsolatedOrchestratorService(
            orchestrator_name=f"bot-bottle-orch-itest-{suffix}",
            gateway_name=f"bot-bottle-gw-itest-{suffix}",
            network=f"bot-bottle-net-itest-{suffix}",
            port=20000 + secrets.randbelow(10000),
            host_root=Path(self._tmp.name),
        )
        self.addCleanup(self.svc.stop)
        self.svc.ensure_running()
        self.token = host_control_plane_token()

    def _request(
        self, method: str, path: str, *, token: str | None = None
    ) -> tuple[int, dict[str, object]]:
        headers = {CONTROL_AUTH_HEADER: token} if token is not None else {}
        req = urllib.request.Request(
            self.svc.url + path, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_is_open_without_a_token(self) -> None:
        status, payload = self._request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_bottles_rejects_a_caller_with_no_token(self) -> None:
        """The enumeration attack from issue #400: an agent sharing the
        gateway network could list every bottle + its policy with no
        credential at all."""
        status, _ = self._request("GET", "/bottles")
        self.assertEqual(401, status)

    def test_bottles_rejects_a_wrong_token(self) -> None:
        status, _ = self._request("GET", "/bottles", token="not-the-real-secret")
        self.assertEqual(401, status)

    def test_bottles_accepts_the_real_token(self) -> None:
        status, payload = self._request("GET", "/bottles", token=self.token)
        self.assertEqual(200, status)
        self.assertEqual([], payload["bottles"])

    def test_resolve_rejects_a_caller_with_no_token(self) -> None:
        """The credential-lift attack from issue #400: an agent could POST
        /resolve directly and read back the upstream tokens it's never meant
        to see."""
        status, _ = self._request("POST", "/resolve")
        self.assertEqual(401, status)


if __name__ == "__main__":
    unittest.main()
