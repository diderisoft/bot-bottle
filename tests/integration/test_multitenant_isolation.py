"""Integration: two bottles share one gateway; source-IP attribution keeps
their egress tokens *and* allowlists isolated (PRD 0070 multi-tenancy).

The whole premise of the consolidation is one shared gateway for every
bottle, isolated only by the unspoofable source IP. A single-bottle test
can't catch cross-bottle token bleed or an allowlist leak — this brings up
the real orchestrator + gateway and drives two bottles through the one
gateway to prove each gets exactly its own token and its own routes.

Gated on a reachable Docker daemon and builds the bundle image, so it runs
with the other docker integration tests, not in unit CI. Uses the real
fixed gateway/orchestrator names (that's what it's testing), so it can't run
concurrently with a live per-host gateway; it points the orchestrator at a
throwaway BOT_BOTTLE_ROOT for a clean registry and tears everything down.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bot_bottle.backend.docker.consolidated_launch import (
    _network_cidr,
    _network_container_ips,
)
from bot_bottle.backend.docker.egress import EGRESS_PORT
from bot_bottle.backend.docker.gateway_net import next_free_ip
from bot_bottle.orchestrator.client import OrchestratorClient
from bot_bottle.gateway import GATEWAY_IMAGE, GATEWAY_NAME, GATEWAY_NETWORK
from bot_bottle.orchestrator.lifecycle import OrchestratorService
from tests._docker import skip_unless_docker

# One upstream reachable under two names; reflects the Authorization header so
# the probe can see exactly what the gateway injected (or didn't).
_ECHO_NAME = "bot-bottle-mtitest-echo"
_ECHO_ALIASES = ("echo-shared", "echo-bonly")
_ECHO_SRC = (
    "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
    "class H(BaseHTTPRequestHandler):\n"
    " def do_GET(s):\n"
    "  a=s.headers.get('Authorization','NONE'); s.send_response(200); s.end_headers()\n"
    "  s.wfile.write(('AUTH='+a).encode())\n"
    " def log_message(s,*a): pass\n"
    "HTTPServer(('0.0.0.0',80),H).serve_forever()\n"
)

# A: echo-shared only, authed.  B: echo-shared authed + echo-bonly open.
_POLICY_A = (
    'routes:\n'
    '  - host: echo-shared\n'
    '    auth_scheme: "Bearer"\n'
    '    token_env: "EGRESS_TOKEN_0"\n'
)
_POLICY_B = _POLICY_A + '  - host: echo-bonly\n'
_TOKEN_A = "sk-AAA-alice"
_TOKEN_B = "sk-BBB-bob"

# Client probe: open http://<host>/ through the gateway proxy from a container
# pinned to the bottle's source IP, printing "<status> <body>". Uses only the
# bundle image's python3 — no dependency on the agent image.
_PROBE_SRC = (
    "import sys,urllib.request,urllib.error\n"
    "op=urllib.request.build_opener(urllib.request.ProxyHandler({'http':sys.argv[1]}))\n"
    "try:\n"
    " r=op.open('http://'+sys.argv[2]+'/',timeout=8)\n"
    " sys.stdout.write(str(r.status)+' '+r.read().decode())\n"
    "except urllib.error.HTTPError as e:\n"
    " sys.stdout.write(str(e.code)+' '+e.read().decode())\n"
)


@skip_unless_docker()
@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: the orchestrator container bind-mounts the repo "
    "path into a container on the socket-shared host daemon, which can't see the "
    "runner's /workspace — same host-bind-mount constraint as the other "
    "bottle-bringup integration tests",
)
class TestMultitenantIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Throwaway root → a clean registry DB, independent of the host's.
        self.svc = OrchestratorService(host_root=Path(self._tmp.name))
        self.addCleanup(self._teardown_docker)
        # ensure_running builds the bundle image (slow on a cold cache) and
        # brings up the shared network + gateway + orchestrator.
        url = self.svc.ensure_running(startup_timeout=300)
        self.client = OrchestratorClient(url)
        self.gw_ip = self._container_ip(GATEWAY_NAME)
        self._run_echo()

    def _teardown_docker(self) -> None:
        subprocess.run(["docker", "rm", "--force", _ECHO_NAME],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self.svc.stop()
        subprocess.run(["docker", "network", "rm", GATEWAY_NETWORK],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        # The orchestrator container wrote the registry DB as root into the
        # throwaway root; chown it back so the (non-root) tempdir cleanup can
        # remove it.
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{self._tmp.name}:/r",
             "--entrypoint", "chown", GATEWAY_IMAGE, "-R",
             f"{os.getuid()}:{os.getgid()}", "/r"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    @staticmethod
    def _container_ip(name: str) -> str:
        proc = subprocess.run(
            ["docker", "inspect", "-f",
             '{{(index .NetworkSettings.Networks "' + GATEWAY_NETWORK + '").IPAddress}}', name],
            stdout=subprocess.PIPE, text=True, check=True,
        )
        return proc.stdout.strip()

    def _run_echo(self) -> None:
        argv = ["docker", "run", "--detach", "--name", _ECHO_NAME,
                "--network", GATEWAY_NETWORK]
        for alias in _ECHO_ALIASES:
            argv += ["--network-alias", alias]
        argv += ["--entrypoint", "python3", GATEWAY_IMAGE, "-c", _ECHO_SRC]
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, check=False)
        self.assertEqual(0, proc.returncode, f"echo upstream failed: {proc.stderr}")

    def _free_ip(self, extra_taken: list[str]) -> str:
        taken = _network_container_ips(GATEWAY_NETWORK) + extra_taken
        return next_free_ip(_network_cidr(GATEWAY_NETWORK), taken)

    def _probe(self, source_ip: str, host: str) -> str:
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", GATEWAY_NETWORK, "--ip", source_ip,
             "--entrypoint", "python3", GATEWAY_IMAGE, "-c", _PROBE_SRC,
             f"http://{self.gw_ip}:{EGRESS_PORT}", host],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=90,
        )
        return proc.stdout.strip()

    def test_two_bottles_share_gateway_with_isolated_tokens_and_allowlists(self) -> None:
        ip_a = self._free_ip([])
        ip_b = self._free_ip([ip_a])
        self.client.register_bottle(ip_a, policy=_POLICY_A, tokens={"EGRESS_TOKEN_0": _TOKEN_A})
        self.client.register_bottle(ip_b, policy=_POLICY_B, tokens={"EGRESS_TOKEN_0": _TOKEN_B})

        # Each bottle gets its OWN token injected on the shared route — no bleed.
        self.assertEqual(f"200 AUTH=Bearer {_TOKEN_A}", self._probe(ip_a, "echo-shared"))
        self.assertEqual(f"200 AUTH=Bearer {_TOKEN_B}", self._probe(ip_b, "echo-shared"))

        # Allowlist is per-bottle: echo-bonly is only in B's policy.
        self.assertTrue(self._probe(ip_a, "echo-bonly").startswith("403"),  # fail-closed for A
                        "A reached a host outside its allowlist")
        self.assertEqual("200 AUTH=NONE", self._probe(ip_b, "echo-bonly"))  # allowed, unauthed for B


if __name__ == "__main__":
    unittest.main()
