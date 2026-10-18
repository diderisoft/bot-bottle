"""Integration: a running bottle survives an orchestrator or gateway restart
(PRD 0081, docker backend).

Brings up the real orchestrator + gateway, runs a stand-in agent as its own
compose project (which is how the reattach flow finds real bottles), and checks
that after each plane restarts the agent's egress token is injected again and
the gateway's CA is installed in the agent. Without the reattach, an
orchestrator restart leaves the bottle's encrypted tokens unreadable and its
egress unauthenticated.

Uses the fixed gateway/orchestrator names, so it can't run alongside a live
per-host gateway; it points the orchestrator at a throwaway BOT_BOTTLE_ROOT
and tears everything down.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from bot_bottle.backend.docker.consolidated_launch import _network_cidr, _network_container_ips
from bot_bottle.backend.docker.egress import EGRESS_PORT
from bot_bottle.backend.docker.gateway_net import next_free_ip
from bot_bottle.backend.docker.infra import DockerInfraService
from bot_bottle.backend.docker.orchestrator import ORCHESTRATOR_NAME
from bot_bottle.backend.util import AGENT_CA_PATH
from bot_bottle.gateway import GATEWAY_IMAGE, GATEWAY_NAME, GATEWAY_NETWORK
from bot_bottle.orchestrator.client import OrchestratorClient
from bot_bottle.orchestrator.store.secret_store import ENV_VAR_SECRET_NAME, new_env_var_secret
from tests._backend import skip_unless_backend

_ECHO_NAME = "bot-bottle-reattach-echo"
_ECHO_SRC = (
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "class H(BaseHTTPRequestHandler):\n"
    " def do_GET(s):\n"
    "  a=s.headers.get('Authorization','NONE'); s.send_response(200); s.end_headers()\n"
    "  s.wfile.write(('AUTH='+a).encode())\n"
    " def log_message(s,*a): pass\n"
    "HTTPServer(('0.0.0.0',80),H).serve_forever()\n"
)
_POLICY = (
    "routes:\n"
    "  - host: echo\n"
    '    auth_scheme: "Bearer"\n'
    '    token_env: "EGRESS_TOKEN_0"\n'
)
_TOKEN = "sk-reattach-" + secrets.token_hex(4)
_PROBE_SRC = (
    "import sys,urllib.request,urllib.error\n"
    "op=urllib.request.build_opener(urllib.request.ProxyHandler({'http':sys.argv[1]}))\n"
    "try:\n"
    " r=op.open('http://echo/',timeout=8)\n"
    " sys.stdout.write(str(r.status)+' '+r.read().decode())\n"
    "except Exception as e:\n"
    " sys.stdout.write('ERR '+str(e))\n"
)


@skip_unless_backend("docker")
class TestGatewayReattachDocker(unittest.TestCase):
    def setUp(self) -> None:
        self._root_volume = "bot-bottle-reattach-root-" + secrets.token_hex(4)
        self._slug = "reattach-" + secrets.token_hex(3)
        self._project = f"bot-bottle-{self._slug}"
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = DockerInfraService(root_mount_source=self._root_volume)
        self.addCleanup(self._teardown)
        url = self.svc.ensure_running(startup_timeout=300)
        self.client = OrchestratorClient(url)
        subprocess.run(
            ["docker", "run", "--detach", "--name", _ECHO_NAME, "--network", GATEWAY_NETWORK,
             "--network-alias", "echo", "--entrypoint", "python3", GATEWAY_IMAGE, "-c", _ECHO_SRC],
            capture_output=True, check=True,
        )

    def _teardown(self) -> None:
        compose = Path(self._tmp.name) / "compose.yml"
        if compose.exists():
            subprocess.run(["docker", "compose", "-p", self._project, "-f", str(compose),
                            "down", "--timeout", "0"], capture_output=True, check=False)
        subprocess.run(["docker", "rm", "--force", _ECHO_NAME], capture_output=True, check=False)
        self.svc.stop()
        subprocess.run(["docker", "network", "rm", GATEWAY_NETWORK], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", "--force", self._root_volume],
                       capture_output=True, check=False)
        self._tmp.cleanup()

    def _start_agent(self, source_ip: str, env_var_secret: str) -> None:
        """A stand-in agent: its own compose project with an `agent` service
        pinned to the registered address, carrying its ENV_VAR_SECRET, exactly
        how a docker bottle is laid out."""
        compose = Path(self._tmp.name) / "compose.yml"
        compose.write_text(json.dumps({
            "name": self._project,
            "services": {"agent": {
                "image": GATEWAY_IMAGE,
                "entrypoint": ["sleep", "infinity"],
                "networks": {GATEWAY_NETWORK: {"ipv4_address": source_ip}},
                "environment": [ENV_VAR_SECRET_NAME],
            }},
            "networks": {GATEWAY_NETWORK: {"external": True}},
        }))
        subprocess.run(
            ["docker", "compose", "-p", self._project, "-f", str(compose), "up", "--detach"],
            capture_output=True, check=True,
            env={**__import__("os").environ, ENV_VAR_SECRET_NAME: env_var_secret},
        )

    def _agent_exec(self, *argv: str) -> str:
        return subprocess.run(
            ["docker", "compose", "-p", self._project, "-f",
             str(Path(self._tmp.name) / "compose.yml"), "exec", "-T", "agent", *argv],
            capture_output=True, text=True, check=False,
        ).stdout

    def _probe(self, identity_token: str, *, want: str) -> str:
        gateway_ip = subprocess.run(
            ["docker", "inspect", "-f",
             '{{(index .NetworkSettings.Networks "' + GATEWAY_NETWORK + '").IPAddress}}',
             GATEWAY_NAME],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        proxy = f"http://bottle:{identity_token}@{gateway_ip}:{EGRESS_PORT}"
        deadline, out = time.monotonic() + 60, ""
        while time.monotonic() < deadline:
            out = self._agent_exec("python3", "-c", _PROBE_SRC, proxy).strip()
            if out == want:
                return out
            time.sleep(0.5)
        return out

    def _gateway_ca(self) -> str:
        return self.svc.gateway().ca_cert_pem()

    def test_orchestrator_and_gateway_restarts_reattach_the_bottle(self) -> None:
        taken = _network_container_ips(GATEWAY_NETWORK)
        source_ip = next_free_ip(_network_cidr(GATEWAY_NETWORK), taken)
        env_var_secret = new_env_var_secret()
        bottle = self.client.register_bottle(
            source_ip, policy=_POLICY, tokens={"EGRESS_TOKEN_0": _TOKEN},
            env_var_secret=env_var_secret,
        )
        self._start_agent(source_ip, env_var_secret)
        want = f"200 AUTH=Bearer {_TOKEN}"
        self.assertEqual(want, self._probe(bottle.identity_token, want=want))

        # Orchestrator restart: its in-memory tokens are gone until the
        # bring-up hands the bottle's ENV_VAR_SECRET back.
        subprocess.run(["docker", "rm", "--force", ORCHESTRATOR_NAME],
                       capture_output=True, check=True)
        self.svc.ensure_running(startup_timeout=300)
        self.assertEqual(want, self._probe(bottle.identity_token, want=want))
        self.assertEqual(self._gateway_ca().strip(),
                         self._agent_exec("cat", AGENT_CA_PATH).strip())

        # Gateway restart: recreated from scratch, then the bottle reattached.
        subprocess.run(["docker", "rm", "--force", GATEWAY_NAME], capture_output=True, check=True)
        self.svc.ensure_running(startup_timeout=300)
        self.assertEqual(want, self._probe(bottle.identity_token, want=want))
        self.assertEqual(self._gateway_ca().strip(),
                         self._agent_exec("cat", AGENT_CA_PATH).strip())


if __name__ == "__main__":
    unittest.main()
