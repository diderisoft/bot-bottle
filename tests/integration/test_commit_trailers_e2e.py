"""Integration: commit trailers end to end (PRD 0112 step 1, design repo).

Launches a real bottle (on the backend `BOT_BOTTLE_BACKEND` names) whose
copied workspace holds an operator commit that was never pushed, with an
upstream it can push to through git-gate: a throwaway SSH git server. Inside
the bottle the agent commits (the provisioned hook adds `Bot-Bottle` and the
operator's `Co-Authored-By`) and pushes. It checks:

  1. the push is accepted, the operator's commit reaches upstream unchanged
     and the agent's commit carries both trailers;
  2. a commit whose trailer was stripped is refused with the fix-it command,
     and that command makes it pushable;
  3. git-gate's `forge.push_accepted` audit record lists the pushed SHAs.

The stand-in upstream: on docker, an Alpine sshd container joined to the
gateway's network; on other backends (firecracker), a second bottle used as a
plain SSH git server: the gateway VM reaches agent guests' SSH through the
host's existing forwarding rules, so no network rule is added or relaxed for
the test.

The audit journal lives with the host controller. On docker the test stands
one in: an HTTP server the orchestrator is pointed at
(`Orchestrator.host_control`; the shared infra is restarted to pick it up and
stopped at the end) that records `POST /audit/append` and says it can't list
live bottles, so reconciliation leaves the registry alone. On other backends
it reads the host controller's journal (`host_audit_dir()`). Test 3 fails
when test 1's push never reached the upstream, and skips only when the pushes
did but no host controller wrote the journal (the infra wasn't started by
one).
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import shlex
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
from bot_bottle.paths import host_audit_dir
from bot_bottle.trust_domain import HostControlProvisioning, HostLink
from tests._backend import selected_backend, skip_unless_selected_backend_available

_OPERATOR = ("Op Erator", "op@example.com")


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kw)


def _git(cwd: Path, *args: str) -> str:
    proc = _run(["git", *args], cwd=cwd)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


# ---- stand-in upstreams ----------------------------------------------------

class _Upstream:
    """An SSH git server holding one bare repo at `url`."""

    url: str = ""
    host_key: str = ""

    def seed(self, bare: Path) -> None:
        raise NotImplementedError

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def attach(self) -> None:
        """Make it reachable from the gateway, once the gateway is up."""

    def stop(self) -> None:
        raise NotImplementedError


_ALPINE_SETUP = """set -eu
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


class _DockerUpstream(_Upstream):
    """Alpine sshd. Packages install on the default bridge; it joins the
    gateway's (internal) network once the gateway exists."""

    alias = "trailers-upstream.test"
    repo = "/srv/git/app.git"

    def __init__(self, pubkey: Path) -> None:
        self.name = "bot-bottle-trailers-upstream-" + secrets.token_hex(3)
        self.url = f"ssh://git@{self.alias}:22{self.repo}"
        proc = _run(["docker", "create", "--name", self.name, "alpine:3.20",
                     "sh", "-c", _ALPINE_SETUP])
        if proc.returncode != 0:
            raise AssertionError(f"upstream create failed: {proc.stderr}")
        _run(["docker", "cp", str(pubkey), f"{self.name}:/tmp/authorized_keys"])
        proc = _run(["docker", "start", self.name])
        if proc.returncode != 0:
            raise AssertionError(f"upstream start failed: {proc.stderr}")
        deadline = time.monotonic() + 180
        while _run(["docker", "exec", self.name, "test", "-f", "/tmp/ready"]).returncode:
            if time.monotonic() > deadline:
                logs = _run(["docker", "logs", self.name])
                raise AssertionError(f"upstream never got ready: {logs.stdout}{logs.stderr}")
            time.sleep(1)
        key = _run(["docker", "exec", self.name, "cat",
                    "/etc/ssh/ssh_host_ed25519_key.pub"]).stdout.split()
        self.host_key = " ".join(key[:2])

    def seed(self, bare: Path) -> None:
        _run(["docker", "cp", str(bare), f"{self.name}:{self.repo}"])
        _run(["docker", "exec", self.name, "chown", "-R", "git:git", "/srv/git"])

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run(["docker", "exec", "-u", "git", self.name, "git", "-C", self.repo, *args])

    def attach(self) -> None:
        proc = _run(["docker", "network", "connect", "--alias", self.alias,
                     GATEWAY_NETWORK, self.name])
        if proc.returncode != 0:
            raise AssertionError(f"upstream couldn't join {GATEWAY_NETWORK}: {proc.stderr}")

    def stop(self) -> None:
        _run(["docker", "rm", "--force", self.name])


class _BottleUpstream(_Upstream):
    """A second bottle on the same backend, used as a plain SSH git server
    (non-docker backends). Its guest SSH server answers on its guest address
    with the per-bottle key, and the gateway VM reaches agent guests through
    the host's existing forwarding rules, so the test needs no new network
    allowance. The bottle's key becomes the test repo's deploy key; both go
    away with the bottle."""

    repo = "/srv/git/app.git"

    def __init__(self, backend: Any, tmp: Path) -> None:
        manifest = ManifestIndex.from_json_obj({
            "bottles": {"upstream": {}},
            "agents": {"trailer-upstream": {"skills": [], "prompt": ""}},
        })
        spec = BottleSpec(manifest=manifest, agent_name="trailer-upstream",
                          bottle_names=("upstream",), copy_cwd=False, user_cwd=str(tmp))
        stage = tmp / "upstream-stage"
        stage.mkdir()
        plan = backend.prepare(spec, stage_dir=stage, secrets=secrets_from_env())
        self.identity = plan.slug
        self._cm = backend.launch(plan)
        self.bottle = self._cm.__enter__()  # pylint: disable=unnecessary-dunder-call
        try:
            self._connect(tmp)
        except BaseException:
            self.stop()
            raise

    def _connect(self, tmp: Path) -> None:
        guest_ip = getattr(self.bottle, "_guest_ip", "")
        bottle_key = getattr(self.bottle, "_private_key", None)
        if not guest_ip or bottle_key is None:
            raise AssertionError(
                f"the {selected_backend()} bottle exposes no guest SSH address and key "
                "to stand in as an upstream")
        self.key = tmp / "upstream-key"
        shutil.copyfile(bottle_key, self.key)
        self.key.chmod(0o600)
        user = "root"
        self.url = f"ssh://{user}@{guest_ip}:22{self.repo}"
        deadline = time.monotonic() + 60
        while True:
            scan = _run(["ssh-keyscan", "-T", "10", "-t", "ed25519", guest_ip])
            line = next((ln for ln in scan.stdout.splitlines()
                         if ln and not ln.startswith("#")), "")
            if line:
                self.host_key = " ".join(line.split()[1:3])
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"ssh-keyscan {guest_ip} found no key: {scan.stderr}")
            time.sleep(1)

    def seed(self, bare: Path) -> None:
        self.bottle.cp_in(str(bare), self.repo)
        # The gateway logs in as root with no env of ours: own the repo as
        # root and trust it system-wide.
        r = self.bottle.exec(f"chown -R 0:0 /srv/git && "
                             f"git config --system --add safe.directory {self.repo}",
                             user="root")
        if r.returncode != 0:
            raise AssertionError(f"upstream seed failed: {r.stderr}")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        script = " ".join(["git", "-C", self.repo, *(shlex.quote(a) for a in args)])
        r = self.bottle.exec(script, user="root")
        return subprocess.CompletedProcess([], r.returncode, r.stdout, r.stderr)

    def stop(self) -> None:
        try:
            self._cm.__exit__(None, None, None)
        finally:
            cleanup_state(self.identity)


# ---- the audit record --------------------------------------------------------

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
            server.events.append(body.get("event", {}))
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


def _journal_events() -> list[dict[str, Any]]:
    """Every record in the host controller's audit journal."""
    out: list[dict[str, Any]] = []
    for segment in sorted(host_audit_dir().glob("*.jsonl")):
        for line in segment.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# ---- the test -------------------------------------------------------------------

@skip_unless_selected_backend_available()
class TestCommitTrailersEndToEnd(unittest.TestCase):
    _bottle: Any = None
    _launch_cm: Any = None
    _identity: str = ""
    _upstream: _Upstream | None = None
    _hostctl: _HostControl | None = None
    _env: Any = None
    agent_sha: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._backend = selected_backend()
        if cls._backend == "macos-container":
            # The stand-in upstream is a second bottle's guest SSH server.
            # Firecracker guests run one (the backend drives them over SSH);
            # Apple containers are driven with `container exec` and have none.
            raise unittest.SkipTest(
                "needs an SSH-capable bottle as the stand-in upstream; "
                "macos-container bottles have no SSH server")
        cls._tmp = Path(tempfile.mkdtemp(prefix="trailers-e2e."))
        cls._settings = settings_from_env()
        try:
            cls._env = mock.patch.dict(os.environ, {
                "GIT_CONFIG_GLOBAL": str(cls._operator_config()), "GIT_CONFIG_NOSYSTEM": "1"})
            cls._env.start()
            if cls._backend == "docker":
                key = cls._tmp / "deploy-key"
                _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
                cls._key = key
                cls._upstream = _DockerUpstream(Path(f"{key}.pub"))
            else:
                upstream = _BottleUpstream(
                    get_bottle_backend(cls._settings, cls._backend), cls._tmp)
                cls._upstream = upstream
                cls._key = upstream.key
            cls._make_workspace()
            if cls._backend == "docker":
                cls._stand_in_host_control()
            cls._launch()
            cls._upstream.attach()
        except BaseException:
            cls._teardown()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._teardown()

    @classmethod
    def _operator_config(cls) -> Path:
        path = cls._tmp / "operator.gitconfig"
        path.write_text(f"[user]\n\tname = {_OPERATOR[0]}\n\temail = {_OPERATOR[1]}\n",
                        encoding="utf-8")
        return path

    @classmethod
    def _make_workspace(cls) -> None:
        """The operator's checkout: upstream's history plus one unpushed
        commit, with `origin` naming the upstream."""
        assert cls._upstream is not None
        ws = cls._workspace = cls._tmp / "workspace"
        _git(cls._tmp, "init", "-q", "-b", "main", str(ws))
        (ws / "README.md").write_text("app\n", encoding="utf-8")
        _git(ws, "add", "README.md")
        _git(ws, "commit", "-q", "-m", "base")
        seed = cls._tmp / "seed.git"
        _git(cls._tmp, "clone", "-q", "--bare", str(ws), str(seed))
        cls._upstream.seed(seed)
        _git(ws, "remote", "add", "origin", cls._upstream.url)
        _git(ws, "update-ref", "refs/remotes/origin/main", "HEAD")
        (ws / "NOTES.md").write_text("operator wip\n", encoding="utf-8")
        _git(ws, "add", "NOTES.md")
        _git(ws, "commit", "-q", "-m", "feat: operator work in progress")
        cls._operator_sha = _git(ws, "rev-parse", "HEAD")

    @classmethod
    def _stand_in_host_control(cls) -> None:
        """Docker only: restart the shared orchestrator with the test's host
        controller; launch starts it again, learning the URL."""
        hostctl = cls._hostctl = _HostControl()
        threading.Thread(target=hostctl.serve_forever, daemon=True).start()
        client_network = cls._settings.infra.docker_client_network
        Orchestrator.host_control = _TestHostControl(
            listen=lambda _link: None, port=hostctl.server_address[1],
            address=socket.gethostname() if client_network else "")
        DockerInfraService(cls._settings).stop()

    @classmethod
    def _launch(cls) -> None:
        assert cls._upstream is not None
        manifest = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"git-gate": {
                "user": {"name": "agent-bot", "email": "agent@example.com"},
                "repos": {"app": {
                    "url": cls._upstream.url,
                    "key": {"provider": "static", "path": str(cls._key)},
                    "host_key": cls._upstream.host_key,
                }},
            }}},
            "agents": {"trailer-tester": {"skills": [], "prompt": ""}},
        })
        spec = BottleSpec(manifest=manifest, agent_name="trailer-tester",
                          bottle_names=("dev",), copy_cwd=True, user_cwd=str(cls._workspace))
        backend = get_bottle_backend(cls._settings, cls._backend)
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
        if cls._hostctl is not None:
            # Later suites start their own orchestrator, without the test's
            # host controller.
            Orchestrator.host_control = None
            DockerInfraService(cls._settings).stop()
            cls._hostctl.shutdown()
            cls._hostctl.server_close()
            cls._hostctl = None
        if cls._upstream is not None:
            cls._upstream.stop()
            cls._upstream = None
        if cls._env is not None:
            cls._env.stop()
            cls._env = None
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ---- helpers -------------------------------------------------------

    def in_bottle(self, script: str) -> subprocess.CompletedProcess[str]:
        r = self._bottle.exec(f"set -eu\ncd {self._guest_ws}\n{script}")
        return subprocess.CompletedProcess([], r.returncode, r.stdout, r.stderr)

    def upstream_git(self, *args: str) -> str:
        assert self._upstream is not None
        proc = self._upstream.git(*args)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout.strip()

    def push(self) -> subprocess.CompletedProcess[str]:
        return self.in_bottle("git push origin HEAD:refs/heads/main 2>&1")

    def pushed_events(self, until: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 30
        while True:
            source = (self._hostctl.events if self._hostctl is not None
                      else _journal_events())
            events = [e for e in source if e.get("type") == "forge.push_accepted"
                      and (e.get("payload") or {}).get("bot_bottle_trailer") == self._trailer]
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
        type(self).agent_sha = agent_sha
        self.assertEqual(self._operator_sha, self.upstream_git("rev-parse", "main~1"),
                         "the operator's commit must reach upstream unchanged")
        operator_body = self.upstream_git("log", "-1", "--format=%B", self._operator_sha)
        self.assertNotIn("Bot-Bottle:", operator_body)
        trailers = self.upstream_git("log", "-1", "--format=%(trailers:only,unfold)", "main")
        self.assertIn(f"Bot-Bottle: {self._trailer}", trailers)
        self.assertIn(f"Co-Authored-By: {_OPERATOR[0]} <{_OPERATOR[1]}>", trailers)
        self.assertTrue(self._trailer.endswith(f"/{self._identity}"), self._trailer)

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
        if not self.agent_sha:
            self.fail("test 1's push never reached the upstream, so there is no "
                      "forge.push_accepted record to check (see test 1's failure)")
        events = self.pushed_events(until=2)
        if not events and self._hostctl is None:
            # The pushes reached upstream, so only the record is missing:
            # nothing wrote the journal.
            self.skipTest(f"no host controller wrote {host_audit_dir()}; start the infra "
                          "from the host controller to check the audit record")
        self.assertGreaterEqual(len(events), 2, events)
        first = events[0]
        self.assertEqual(self._identity, first["bottle"])
        self.assertEqual("refs/heads/main", first["payload"]["ref"])
        self.assertEqual([self._operator_sha, self.agent_sha], first["payload"]["commits"])
        self.assertEqual(self._trailer, first["payload"]["bot_bottle_trailer"])
        self.assertTrue(first["ts_event"])
        self.assertEqual([self.upstream_git("rev-parse", "main")],
                         events[-1]["payload"]["commits"])


if __name__ == "__main__":
    unittest.main()
