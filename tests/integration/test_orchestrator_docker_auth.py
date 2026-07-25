"""Integration: the Docker orchestrator's control plane enforces the
per-host auth secret against a real container (issue #400).

Unit tests exercise `dispatch()` in-process, socket-free. This starts the
actual orchestrator + gateway as Docker containers and drives the real HTTP
server over its published loopback port, the same path an agent sharing the
gateway network — or the trusted host CLI — would use.

Gated on a reachable Docker daemon. Uses unique container/network names,
test-only image tags (never the production `:latest` ones, so a rebuild
here can't make `_running_image_is_current()` see a real host's running
gateway as stale and force-recreate it), and a throwaway `BOT_BOTTLE_ROOT`
so a run never touches or collides with a real per-host orchestrator or
gateway. The whole stack is brought up once for the class (`setUpClass`),
not per test method — every test here is a read-only check against the
same running control plane.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path

from bot_bottle.orchestrator_auth import ROLE_CLI, ROLE_GATEWAY, mint
from bot_bottle.orchestrator.client import OrchestratorClient
from bot_bottle.backend.docker.infra import DockerInfraService
from bot_bottle.paths import host_orchestrator_token
from tests._backend import skip_unless_backend

# Fixed (not per-run-suffixed) so repeated runs reuse the same layer-cached
# image instead of leaking a new dangling tag on every invocation.
_TEST_ORCHESTRATOR_IMAGE = "bot-bottle-orchestrator:itest"
_TEST_GATEWAY_IMAGE = "bot-bottle-gateway:itest"


@skip_unless_backend("docker")
@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: the orchestrator container bind-mounts the repo "
    "path into a container on the socket-shared host daemon, which can't see the "
    "runner's /workspace — same host-bind-mount constraint as the other "
    "bottle-bringup integration tests",
)
class TestDockerOrchestratorAuthIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        suffix = secrets.token_hex(4)
        cls._tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        cls.addClassCleanup(cls._tmp.cleanup)

        # host_orchestrator_token() — both the token read below and the one
        # DockerInfraService injects into the container's env — resolves its
        # path via the *ambient* BOT_BOTTLE_ROOT env var, not the host_root
        # kwarg passed to the constructor (that kwarg only controls the DB
        # bind-mount destination). Without pointing the env var at the same
        # throwaway dir, this "isolated" test would read/write the developer's
        # real ~/.bot-bottle/orchestrator-token.
        previous_root = os.environ.get("BOT_BOTTLE_ROOT")

        def _restore_root() -> None:
            if previous_root is None:
                os.environ.pop("BOT_BOTTLE_ROOT", None)
            else:
                os.environ["BOT_BOTTLE_ROOT"] = previous_root

        os.environ["BOT_BOTTLE_ROOT"] = cls._tmp.name
        cls.addClassCleanup(_restore_root)

        orchestrator_name = f"bot-bottle-orchestrator-itest-{suffix}"
        gateway_name = f"bot-bottle-gateway-itest-{suffix}"
        network = f"bot-bottle-net-itest-{suffix}"
        control_network = f"bot-bottle-ctrl-itest-{suffix}"
        host_root = Path(cls._tmp.name)
        cls.addClassCleanup(
            cls._teardown_docker,
            orchestrator_name, gateway_name, network, control_network, host_root,
        )

        cls.svc = DockerInfraService(
            orchestrator_name=orchestrator_name,
            gateway_name=gateway_name,
            network=network,
            control_network=control_network,
            orchestrator_image=_TEST_ORCHESTRATOR_IMAGE,
            gateway_image=_TEST_GATEWAY_IMAGE,
            port=20000 + secrets.randbelow(10000),
            host_root=host_root,
        )
        cls.svc.ensure_running()
        # The control plane now verifies role-scoped signed tokens, not the raw
        # key. Mint one of each role from the host signing key (issue #469 review).
        signing_key = host_orchestrator_token()
        cls.cli_token = mint(ROLE_CLI, signing_key)
        cls.gateway_token = mint(ROLE_GATEWAY, signing_key)

    @staticmethod
    def _teardown_docker(
        orchestrator_name: str, gateway_name: str,
        network: str, control_network: str, host_root: Path,
    ) -> None:
        for name in (gateway_name, orchestrator_name):
            subprocess.run(
                ["docker", "rm", "--force", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        for net in (network, control_network):
            subprocess.run(
                ["docker", "network", "rm", net],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        # The orchestrator container (no USER directive) wrote the registry
        # DB as root into the throwaway host_root; chown it back so the
        # (non-root) tempdir cleanup can remove it. Same workaround
        # test_multitenant_isolation.py uses for the identical bind mount.
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_root}:/r",
             "--entrypoint", "chown", _TEST_ORCHESTRATOR_IMAGE, "-R",
             f"{os.getuid()}:{os.getgid()}", "/r"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )

    def _request(
        self, method: str, path: str, *, token: str = ""
    ) -> tuple[int, dict[str, object]]:
        # Reuses the real host-side client's request/response handling rather
        # than hand-rolling urllib here; _request (not one of the named
        # wrapper methods) is what exposes raw status codes for arbitrary
        # paths/tokens, which is exactly what these auth-boundary tests need.
        client = OrchestratorClient(self.svc.url, auth_token=token)
        return client._request(method, path)  # pylint: disable=protected-access

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

    def test_bottles_accepts_the_cli_token(self) -> None:
        status, payload = self._request("GET", "/bottles", token=self.cli_token)
        self.assertEqual(200, status)
        self.assertEqual([], payload["bottles"])

    def test_bottles_rejects_the_gateway_token(self) -> None:
        """A compromised gateway holds only a `gateway` token — it must not be
        able to enumerate bottles (an operator route) with it (issue #469)."""
        status, _ = self._request("GET", "/bottles", token=self.gateway_token)
        self.assertEqual(403, status)

    def test_resolve_rejects_a_caller_with_no_token(self) -> None:
        """The credential-lift attack from issue #400: an agent could POST
        /resolve directly and read back the upstream tokens it's never meant
        to see."""
        status, _ = self._request("POST", "/resolve")
        self.assertEqual(401, status)

    def test_resolve_accepts_the_gateway_token(self) -> None:
        """The gateway's own token DOES reach /resolve: with an empty body it
        gets 400 (missing source_ip) rather than the 401 a rejected caller sees
        — proving the role gate let the gateway token through."""
        status, _ = self._request("POST", "/resolve", token=self.gateway_token)
        self.assertEqual(400, status)  # past the role gate; body validation fails


if __name__ == "__main__":
    unittest.main()
