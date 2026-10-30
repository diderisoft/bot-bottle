"""Integration: commit trailers end to end (PRD 0112 step 1, design repo).

Launches a real Docker bottle whose copied workspace holds an operator
commit that was never pushed, and an upstream it can push to through
git-gate: a throwaway SSH git server on the gateway's network. Inside the
bottle the agent commits (the provisioned hook adds `Bot-Bottle` and the
operator's `Co-Authored-By`) and pushes. It checks:

  1. the push is accepted, the operator's commit reaches upstream unchanged
     and the agent's commit carries both trailers;
  2. a commit whose trailer was stripped is refused with the fix-it command,
     and that command makes it pushable;
  3. git-gate's `forge.push_accepted` audit record lists the pushed SHAs.

The audit journal lives with the host controller, so the test stands one in:
an HTTP server the orchestrator is pointed at (`Orchestrator.host_control`)
that records `POST /audit/append` and says it can't list live bottles (so
reconciliation leaves the registry alone). It restarts the shared infra to
pick that up, and stops it again at the end.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.backend import BottleSpec, get_bottle_backend
from bot_bottle.backend.docker.infra import DockerInfraService
from bot_bottle.bottle_state import cleanup_state
from bot_bottle.cli.secrets_env import secrets_from_env
from bot_bottle.cli.settings import settings_from_env
from bot_bottle.contract.hostctl import AUDIT_APPEND_PATH, LIVE_BOTTLES_PATH
from bot_bottle.control.gateway import GATEWAY_NETWORK
from bot_bottle.control.lifecycle import Orchestrator
from bot_bottle.manifest import ManifestIndex
from bot_bottle.trust_domain import HostControlProvisioning, HostLink
from tests._backend import skip_unless_backend

_UPSTREAM_NAME = "bot-bottle-trailers-upstream-" + secrets.token_hex(3)
_UPSTREAM_HOST = "trailers-upstream.test"
_UPSTREAM_IMAGE = "alpine:3.20"
_REPO_PATH = "/srv/git/app.git"
_UPSTREAM_URL = f"ssh://git@{_UPSTREAM_HOST}:22{_REPO_PATH}"
_OPERATOR = ("Op Erator", "op@example.com")

# The stand-in forge: sshd, a `git` user with the test's key, a bare repo.
_UPSTREAM_SETUP = """set -eu
apk add --no-cache openssh-server git >/dev/null
ssh-keygen -A >/dev/null
adduser -D -s /bin/sh git
sed -i 's/^git:!/git:*/' /etc/shadow
mkdir -p /home/git/.ssh /srv/git
cp /tmp/authorized_keys /home/git/.ssh/authorized_keys
chown -R git:git /home/git/.ssh /srv/git
chmod 700 /home/git/.ssh && chmod 600 /home/git/.ssh/authorized_keys
touch /tmp/ready
exec /usr/sbin/sshd -D -e
"""


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kw)


def _git(cwd: Path, *args: str) -> str:
    proc = _run(["git", *args], cwd=cwd)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


class _HostControl(ThreadingHTTPServer):
    """Records audit events; can't list live bottles."""

    def __init__(self) -> None:
        super().__init__(("0.0.0.0", 0), _Handler)
        self.events: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        if self.path == LIVE_BOTTLES_PATH:
            self._reply(503, {"error": "test host controller lists no bottles"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == AUDIT_APPEND_PATH:
            server: Any = self.server
            server.events.append(body)
            self._reply(200, {})
        else:
            self._reply(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        del format, args


@dataclasses.dataclass(frozen=True)
class _TestHostControl(HostControlProvisioning):
    """Points the orchestrator at the test's server: by this container's name
    when the run shares a Docker network with it (a CI job container), else
    at the host's address on the orchestrator's network."""

    address: str = ""

    def url(self, link: HostLink) -> str:
        return f"http://{self.address or link.address}:{self.port}"


@skip_unless_backend("docker")
class TestCommitTrailersEndToEnd(unittest.TestCase):
    _bottle: Any = None
    _launch_cm: Any = None
    _identity: str = ""
    agent_sha: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="trailers-e2e."))
        cls._settings = settings_from_env()
        cls._hostctl = _HostControl()
        threading.Thread(target=cls._hostctl.serve_forever, daemon=True).start()
        try:
            cls._start_infra()
            cls._start_upstream()
            cls._make_workspace()
            cls._launch()
        except BaseException:
            cls._teardown()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._teardown()

    # ---- fixtures ------------------------------------------------------

    @classmethod
    def _start_infra(cls) -> None:
        """Restart the shared orchestrator and gateway with the test's host
        controller; the orchestrator learns its URL when it starts."""
        client_network = cls._settings.infra.docker_client_network
        Orchestrator.host_control = _TestHostControl(
            listen=lambda _link: None, port=cls._hostctl.server_address[1],
            address=socket.gethostname() if client_network else "")
        infra = DockerInfraService(cls._settings)
        infra.stop()
        infra.ensure_running(startup_timeout=600)

    @classmethod
    def _start_upstream(cls) -> None:
        cls._key = cls._tmp / "deploy-key"
        _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls._key)])
        proc = _run(["docker", "create", "--name", _UPSTREAM_NAME,
                     "--network", GATEWAY_NETWORK, "--network-alias", _UPSTREAM_HOST,
                     _UPSTREAM_IMAGE, "sh", "-c", _UPSTREAM_SETUP])
        if proc.returncode != 0:
            raise AssertionError(f"upstream create failed: {proc.stderr}")
        _run(["docker", "cp", f"{cls._key}.pub", f"{_UPSTREAM_NAME}:/tmp/authorized_keys"])
        proc = _run(["docker", "start", _UPSTREAM_NAME])
        if proc.returncode != 0:
            raise AssertionError(f"upstream start failed: {proc.stderr}")
        deadline = time.monotonic() + 180
        while _run(["docker", "exec", _UPSTREAM_NAME, "test", "-f", "/tmp/ready"]).returncode:
            if time.monotonic() > deadline:
                logs = _run(["docker", "logs", _UPSTREAM_NAME])
                raise AssertionError(f"upstream never got ready: {logs.stdout}{logs.stderr}")
            time.sleep(1)
        host_key = _run(["docker", "exec", _UPSTREAM_NAME, "cat",
                         "/etc/ssh/ssh_host_ed25519_key.pub"]).stdout.split()
        cls._host_key = " ".join(host_key[:2])

    @classmethod
    def _make_workspace(cls) -> None:
        """The operator's checkout: upstream's history plus one unpushed
        commit, with `origin` naming the upstream."""
        cls._operator_config = cls._tmp / "operator.gitconfig"
        cls._operator_config.write_text(
            f"[user]\n\tname = {_OPERATOR[0]}\n\temail = {_OPERATOR[1]}\n", encoding="utf-8")
        cls._env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(cls._operator_config), "GIT_CONFIG_NOSYSTEM": "1"})
        cls._env.start()
        ws = cls._workspace = cls._tmp / "workspace"
        _git(cls._tmp, "init", "-q", "-b", "main", str(ws))
        (ws / "README.md").write_text("app\n", encoding="utf-8")
        _git(ws, "add", "README.md")
        _git(ws, "commit", "-q", "-m", "base")
        seed = cls._tmp / "seed.git"
        _git(cls._tmp, "clone", "-q", "--bare", str(ws), str(seed))
        _run(["docker", "cp", str(seed), f"{_UPSTREAM_NAME}:{_REPO_PATH}"])
        _run(["docker", "exec", _UPSTREAM_NAME, "chown", "-R", "git:git", "/srv/git"])
        _git(ws, "remote", "add", "origin", _UPSTREAM_URL)
        _git(ws, "update-ref", "refs/remotes/origin/main", "HEAD")
        (ws / "NOTES.md").write_text("operator wip\n", encoding="utf-8")
        _git(ws, "add", "NOTES.md")
        _git(ws, "commit", "-q", "-m", "feat: operator work in progress")
        cls._operator_sha = _git(ws, "rev-parse", "HEAD")

    @classmethod
    def _launch(cls) -> None:
        manifest = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"git-gate": {
                "user": {"name": "agent-bot", "email": "agent@example.com"},
                "repos": {"app": {
                    "url": _UPSTREAM_URL,
                    "key": {"provider": "static", "path": str(cls._key)},
                    "host_key": cls._host_key,
                }},
            }}},
            "agents": {"trailer-tester": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        spec = BottleSpec(manifest=manifest, agent_name="trailer-tester",
                          copy_cwd=True, user_cwd=str(cls._workspace))
        backend = get_bottle_backend(cls._settings, "docker")
        stage = cls._tmp / "stage"
        stage.mkdir()
        plan = backend.prepare(spec, stage_dir=stage, secrets=secrets_from_env())
        cls._identity = plan.slug
        cls._trailer = plan.git_gate_plan.trailer
        cls._guest_ws = plan.workspace_plan.guest_path
        cls._launch_cm = backend.launch(plan)
        cls._bottle = cls._launch_cm.__enter__()  # pylint: disable=unnecessary-dunder-call

    @classmethod
    def _teardown(cls) -> None:
        if cls._launch_cm is not None:
            try:
                cls._launch_cm.__exit__(None, None, None)
            except BaseException:  # pylint: disable=broad-exception-caught
                pass
            cls._launch_cm = cls._bottle = None
        if cls._identity:
            try:
                cleanup_state(cls._identity)
            except BaseException:  # pylint: disable=broad-exception-caught
                pass
            cls._identity = ""
        Orchestrator.host_control = None
        # Later suites start their own orchestrator, without the test's
        # host controller.
        DockerInfraService(cls._settings).stop()
        _run(["docker", "rm", "--force", _UPSTREAM_NAME])
        env = getattr(cls, "_env", None)
        if env is not None:
            env.stop()
        cls._hostctl.shutdown()
        cls._hostctl.server_close()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ---- helpers -------------------------------------------------------

    def in_bottle(self, script: str) -> subprocess.CompletedProcess[str]:
        r = self._bottle.exec(f"set -eu\ncd {self._guest_ws}\n{script}")
        return subprocess.CompletedProcess([], r.returncode, r.stdout, r.stderr)

    def upstream_git(self, *args: str) -> str:
        proc = _run(["docker", "exec", "-u", "git", _UPSTREAM_NAME, "git",
                     "-C", _REPO_PATH, *args])
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout.strip()

    def push(self) -> subprocess.CompletedProcess[str]:
        return self.in_bottle("git push origin HEAD:refs/heads/main 2>&1")

    def pushed_events(self, until: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 30
        while True:
            events = [b["event"] for b in self._hostctl.events
                      if b.get("event", {}).get("type") == "forge.push_accepted"]
            if len(events) >= until or time.monotonic() > deadline:
                return events
            time.sleep(0.5)

    # ---- tests ---------------------------------------------------------

    def test_1_agent_commit_pushes_with_trailers(self) -> None:
        r = self.in_bottle(
            "echo agent >> README.md && git commit -q -am 'feat: agent work'\n"
            "git rev-parse HEAD")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        agent_sha = r.stdout.strip().splitlines()[-1]
        r = self.push()
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)

        self.assertEqual(agent_sha, self.upstream_git("rev-parse", "main"))
        self.assertEqual(self._operator_sha, self.upstream_git("rev-parse", "main~1"),
                         "the operator's commit must reach upstream unchanged")
        operator_body = self.upstream_git("log", "-1", "--format=%B", self._operator_sha)
        self.assertNotIn("Bot-Bottle:", operator_body)
        trailers = self.upstream_git("log", "-1", "--format=%(trailers:only,unfold)", "main")
        self.assertIn(f"Bot-Bottle: {self._trailer}", trailers)
        self.assertIn(f"Co-Authored-By: {_OPERATOR[0]} <{_OPERATOR[1]}>", trailers)
        self.assertTrue(self._trailer.endswith(f"/{self._identity}"), self._trailer)
        type(self).agent_sha = agent_sha

    def test_2_stripped_trailer_is_refused_with_the_fix(self) -> None:
        # The hook adds the trailer; an amend with the hooks switched off
        # rewrites the message without it.
        r = self.in_bottle(
            "git commit -q --allow-empty -m 'feat: sneaky'\n"
            "GIT_CONFIG_COUNT=0 git commit -q --amend --allow-empty -m 'feat: sneaky'\n"
            "git log -1 --format=%B")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertNotIn("Bot-Bottle:", r.stdout)
        before = self.upstream_git("rev-parse", "main")

        r = self.push()
        self.assertNotEqual(0, r.returncode, r.stdout)
        self.assertIn("don't carry this bottle's trailer", r.stdout)
        self.assertIn("feat: sneaky  (no Bot-Bottle trailer)", r.stdout)
        fix = next(line.split("git-gate:   ", 1)[1] for line in r.stdout.splitlines()
                   if "git rebase --rebase-merges --exec" in line)
        self.assertEqual(before, self.upstream_git("rev-parse", "main"))

        r = self.in_bottle(f"{fix}\ngit push origin HEAD:refs/heads/main 2>&1")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        trailers = self.upstream_git("log", "-1", "--format=%(trailers:only,unfold)", "main")
        self.assertIn(f"Bot-Bottle: {self._trailer}", trailers)

    def test_3_audit_record_lists_pushed_commits(self) -> None:
        events = self.pushed_events(until=2)
        self.assertGreaterEqual(len(events), 2, self._hostctl.events)
        first = events[0]
        self.assertEqual(self._identity, first["bottle"])
        self.assertEqual("refs/heads/main", first["payload"]["ref"])
        self.assertEqual([self._operator_sha, self.agent_sha],
                         first["payload"]["commits"])
        self.assertEqual(self._trailer, first["payload"]["bot_bottle_trailer"])
        self.assertTrue(first["ts_event"])
        last = events[-1]
        self.assertEqual([self.upstream_git("rev-parse", "main")],
                         last["payload"]["commits"])


if __name__ == "__main__":
    unittest.main()
