"""Unit: the host controller's bottles API (PRD 0092 chunk 5): the launch
request schema, the launch job through a fake backend and a real tmux
session, exit codes, teardown, DELETE and the re-adoption finisher. The tmux
tests skip where tmux 3.1+ isn't installed."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle import bottle_state
from bot_bottle.contract.auth import ROLE_CLI, ROLE_GATEWAY
from bot_bottle.contract.hostctl import (
    BOTTLES_PATH, CONFIRM_ERROR, HOST_CONTROL, LAUNCH_CLI_ONLY, ROLE_ORCHESTRATOR,
    SESSIONS_PATH, LaunchRequest, Settings, bearer, exit_code,
)
from bot_bottle.control.hostctl import localsock, sessions
from bot_bottle.control.hostctl.bottles import BottleService, ConfirmationNeeded
from bot_bottle.control.hostctl.jobs import JobRegistry
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.image_cache import StaleImageError
from bot_bottle.lib.log import Die

from . import use_bottle_root

_TMUX_VERSION = sessions.tmux_version() if shutil.which("tmux") else None
_TMUX_OK = _TMUX_VERSION is not None and _TMUX_VERSION >= sessions.MIN_TMUX_VERSION


class TestLaunchRequestSchema(unittest.TestCase):
    def test_round_trips(self) -> None:
        request = LaunchRequest(agent="demo", bottles=("dev", "extra"), label="L", color="red",
                                prompt="go", headless=True, identity="demo-abc",
                                image_policy="cached", assume_yes=True, cwd="/w", copy_cwd=True,
                                secrets={"T": "v"}, host_keys={"r": "ssh-ed25519 AAAA"})
        self.assertEqual(request, LaunchRequest.from_json(request.to_json(), ROLE_CLI))

    def test_secret_values_stay_out_of_the_repr(self) -> None:
        self.assertNotIn("hunter2", repr(LaunchRequest(agent="demo", secrets={"T": "hunter2"})))

    def test_cli_only_fields_are_refused_from_other_roles(self) -> None:
        for name in LAUNCH_CLI_ONLY:
            value = {"cwd": "/w", "copy_cwd": False, "secrets": {}, "host_keys": {}}[name]
            with self.subTest(name), self.assertRaises(PermissionError):
                LaunchRequest.from_json({"agent": "demo", name: value}, ROLE_ORCHESTRATOR)
        LaunchRequest.from_json({"agent": "demo", "prompt": "hi"}, ROLE_ORCHESTRATOR)

    def test_rejects_malformed_requests(self) -> None:
        bad: list[object] = [
            [], {}, {"agent": "demo", "nope": 1}, {"agent": 3},
            {"agent": "../etc"}, {"agent": "demo", "bottles": "dev"},
            {"agent": "demo", "bottles": ["a/b"]}, {"agent": "demo", "color": "teal"},
            {"agent": "demo", "image_policy": "stale"}, {"agent": "demo", "identity": "Bad ID"},
            {"agent": "demo", "headless": True}, {"agent": "demo", "headless": 1, "prompt": "x"},
            {"agent": "demo", "cwd": "rel/path"}, {"agent": "demo", "copy_cwd": True},
            {"agent": "demo", "secrets": {"T": 1}}, {"agent": "demo", "settings": {"x": {}}},
        ]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                LaunchRequest.from_json(body, ROLE_CLI)

    def test_exit_code(self) -> None:
        self.assertEqual(4, exit_code(4))
        self.assertEqual(129, exit_code(0, "SIGHUP"))
        self.assertEqual(128, exit_code(0, "SIGNOPE"))


# -- a fake backend ------------------------------------------------------------


class FakeBottle:
    name = "fake-container"

    def __init__(self, script: str) -> None:
        self.script = script
        self.argv: list[str] = []

    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        self.argv = list(argv)
        return ["sh", "-c", self.script], {"FAKE_SECRET": "s3cret"}


class FakeBackend:
    name = "fake"

    def __init__(self, script: str = "echo up; exit 4") -> None:
        self.bottle = FakeBottle(script)
        self.stale = False
        self.fail_launch = False
        self.specs: list[Any] = []
        self.secrets: list[Any] = []
        self.torn_down = threading.Event()
        self.launched = threading.Event()

    def prepare(self, spec: Any, *, stage_dir: Path, secrets: Any) -> Any:
        del stage_dir
        self.specs.append(spec)
        self.secrets.append(secrets)
        slug = spec.identity or spec.label or "demo-xyz12"
        bottle_state.write_per_bottle_dockerfile(slug, "FROM x\n")
        return SimpleNamespace(
            slug=slug, agent_provider_template="claude",
            agent_provision=SimpleNamespace(startup_args=("--start",)),
            manifest=SimpleNamespace(agent=SimpleNamespace(remote_writable=True)),
        )

    def prelaunch_checks(self, plan: Any) -> None:
        del plan
        if self.stale:
            raise StaleImageError("image is 9 day(s) old")

    @contextlib.contextmanager
    def launch(self, plan: Any) -> Generator[FakeBottle]:
        del plan
        if self.fail_launch:
            raise RuntimeError("no network")
        self.launched.set()
        try:
            yield self.bottle
        finally:
            self.torn_down.set()


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        # Short root: Unix socket paths are limited to ~104 bytes on macOS.
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="bbb.")))
        self.addCleanup(use_bottle_root(root))
        self.addCleanup(lambda: subprocess.run(sessions.tmux("kill-server"),
                                               capture_output=True, check=False))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.manager = sessions.SessionManager({})
        self.backend = FakeBackend()
        self.index = mock.MagicMock()
        self.service = BottleService(self.manager, backend_for=lambda _s: cast(Any, self.backend),
                                     manifest_for=lambda _cwd: self.index,
                                     secrets_file=root / "secrets")


class TestLaunchConfirmation(_Root):
    """Everything before the holder: no tmux needed."""

    def test_unconfirmed_launch_logs_the_preflight_and_asks(self) -> None:
        lines: list[str] = []
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value="agent:"), \
             mock.patch("bot_bottle.lib.log.current_sink", return_value=lines.append), \
             mock.patch("bot_bottle.lib.log.write_line", side_effect=lines.append):
            with self.assertRaises(ConfirmationNeeded) as cm:
                self.service.launch(LaunchRequest(agent="demo", label="c1", cwd="/w"))
        self.assertEqual("launch this agent?", str(cm.exception))
        self.assertIn("backend: fake", lines)
        self.assertFalse(self.backend.launched.is_set())
        # The aborted launch's state dir is reaped.
        self.assertFalse(bottle_state.bottle_state_dir("c1").exists())

    def test_the_spec_is_non_interactive_and_secrets_come_from_the_request(self) -> None:
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""), \
             self.assertRaises(ConfirmationNeeded):
            self.service.launch(LaunchRequest(
                agent="demo", bottles=("dev",), label="c2", cwd="/w", copy_cwd=True,
                secrets={"TOKEN": "v"}, host_keys={"repo": "ssh-ed25519 AAAA"}))
        spec = self.backend.specs[0]
        self.assertTrue(spec.headless)
        self.assertEqual(("demo", ("dev",), "/w", True),
                         (spec.agent_name, spec.bottle_names, spec.user_cwd, spec.copy_cwd))
        self.assertEqual((("repo", "ssh-ed25519 AAAA"),), spec.host_keys)
        self.assertEqual("v", self.backend.secrets[0].get("TOKEN"))
        self.index.require_agent.assert_called_once_with("demo")

    def test_a_stale_image_asks_interactive_callers_and_fails_headless_ones(self) -> None:
        self.backend.stale = True
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""):
            with self.assertRaises(ConfirmationNeeded) as cm:
                self.service.launch(LaunchRequest(agent="demo", label="s1"))
            self.assertIn("Launch anyway?", str(cm.exception))
            with self.assertRaises(Die):
                self.service.launch(LaunchRequest(agent="demo", label="s2", headless=True,
                                                  prompt="go", assume_yes=True))
        self.assertFalse(self.backend.launched.is_set())

    def test_the_job_error_carries_the_confirm_code(self) -> None:
        jobs = JobRegistry()
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""):
            job = jobs.start("bottle.launch", ROLE_CLI,
                             lambda: self.service.launch(LaunchRequest(agent="demo", label="j1")))
            final = list(job.follow(timeout=10))[-1]
        self.assertEqual(("error", CONFIRM_ERROR, "launch this agent?"),
                         (final["type"], final.get("code"), final["message"]))

    def test_a_failed_launch_is_the_jobs_error(self) -> None:
        self.backend.fail_launch = True
        with self.assertRaises(RuntimeError):
            self.service.launch(LaunchRequest(agent="demo", label="f1", assume_yes=True))
        self.assertFalse(bottle_state.bottle_state_dir("f1").exists())
        self.assertEqual([], self.service.list())


@unittest.skipUnless(_TMUX_OK, "needs tmux 3.1+")
class TestHeldBottles(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.local = localsock.serve(sessions.control_socket_path(), self.manager)
        self.addCleanup(self.local.close)

    def launch(self, label: str) -> dict[str, object]:
        return self.service.launch(LaunchRequest(agent="demo", label=label, assume_yes=True))

    def test_agent_exit_tears_down_and_reports_its_code(self) -> None:
        self.assertEqual({"bottle": "b1", "session": "b1"}, self.launch("b1"))
        self.assertEqual(sessions.ExitStatus(4), self.manager.wait_exit("b1", timeout=15))
        # The exit is recorded only after the holder tore the bottle down.
        self.assertTrue(self.backend.torn_down.is_set())
        self.assertEqual({"running": False, "exit": {"status": 4, "signal": ""}},
                         self.service.session_state("b1"))
        self.assertEqual("--start", self.backend.bottle.argv[-1])
        # claude exited non-zero, so its state is kept for `resume`.
        self.assertTrue(bottle_state.is_preserved("b1"))
        self.assertEqual([], self.service.list())

    def test_list_and_delete(self) -> None:
        self.backend.bottle.script = "echo up; sleep 30"
        self.launch("b2")
        listed = self.service.list()
        self.assertEqual([("b2", "fake", "demo", True, True)],
                         [(b["id"], b["backend"], b["agent"], b["held"],
                           cast(dict[str, object], b["session"])["running"]) for b in listed])
        self.assertTrue(self.manager.record("b2").writable_allowed)  # type: ignore[union-attr]
        result = self.service.teardown("b2")
        self.assertEqual({"bottle": "b2", "exit": {"status": 0, "signal": "SIGHUP"}}, result)
        self.assertTrue(self.backend.torn_down.is_set())
        self.assertFalse(self.manager.exists("b2"))
        with self.assertRaises(Die):
            self.service.teardown("b2")

    def test_a_running_label_is_refused(self) -> None:
        self.backend.bottle.script = "sleep 30"
        self.launch("b3")
        with self.assertRaises(Die):
            self.launch("b3")
        self.service.teardown("b3")

    def test_re_adopted_bottle_is_settled_and_left_for_cleanup(self) -> None:
        bottle_state.write_per_bottle_dockerfile("b4", "FROM x\n")
        record = sessions.SessionRecord(id="b4", kind="bottle",
                                        meta={"backend": "fake", "provider": "claude"})
        self.manager.start(record, ["sh", "-c", "sleep 30"], {})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.service.teardown("b4")
        self.assertIn("bot-bottle cleanup", err.getvalue())
        # Stopped by SIGHUP (exit 129): kept for `resume`, like any crash.
        self.assertTrue(bottle_state.is_preserved("b4"))

    def test_re_adopted_clean_exit_is_settled(self) -> None:
        bottle_state.write_per_bottle_dockerfile("b6", "FROM x\n")
        record = sessions.SessionRecord(id="b6", kind="bottle", meta={"provider": "claude"})
        self.manager.start(record, ["sh", "-c", "exit 0"], {})
        self.assertEqual(sessions.ExitStatus(0), self.manager.wait_exit("b6", timeout=15))
        self.assertFalse(bottle_state.bottle_state_dir("b6").exists())

    def test_re_adopted_crash_is_preserved(self) -> None:
        record = sessions.SessionRecord(id="b5", kind="bottle", meta={"provider": "claude"})
        self.manager.start(record, ["sh", "-c", "exit 3"], {})
        self.assertEqual(sessions.ExitStatus(3), self.manager.wait_exit("b5", timeout=15))
        self.assertTrue(bottle_state.is_preserved("b5"))


@unittest.skipUnless(_TMUX_OK, "needs tmux 3.1+")
class TestBottleRoutes(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.local = localsock.serve(sessions.control_socket_path(), self.manager)
        self.addCleanup(self.local.close)
        self.key = "k" * 32
        self.server = HostControlServer(("127.0.0.1", 0), "b", self.key, self.manager,
                                        self.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method: str, path: str, role: str | None = ROLE_CLI,
             body: object = None) -> tuple[int, dict[str, Any]]:
        headers = bearer(HOST_CONTROL.mint(role, self.key)) if role else {}
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def final(self, job_id: str, role: str = ROLE_CLI) -> dict[str, Any]:
        job = self.server.jobs.get(job_id, role)
        assert job is not None
        return list(job.follow(timeout=30))[-1]

    def test_roles_and_validation(self) -> None:
        self.assertEqual(401, self.call("POST", BOTTLES_PATH, None, {"agent": "demo"})[0])
        self.assertEqual(403, self.call("POST", BOTTLES_PATH, ROLE_GATEWAY, {"agent": "demo"})[0])
        self.assertEqual(403, self.call("POST", BOTTLES_PATH, ROLE_ORCHESTRATOR,
                                        {"agent": "demo", "cwd": "/w"})[0])
        self.assertEqual(400, self.call("POST", BOTTLES_PATH, ROLE_CLI, {"agent": "../x"})[0])
        self.assertEqual(403, self.call("GET", BOTTLES_PATH, ROLE_GATEWAY)[0])
        self.assertEqual(403, self.call("GET", f"{SESSIONS_PATH}/x", ROLE_GATEWAY)[0])
        self.assertEqual(404, self.call("DELETE", f"{BOTTLES_PATH}/nope")[0])

    def test_launch_exit_code_and_delete_over_http(self) -> None:
        code, body = self.call("POST", BOTTLES_PATH, ROLE_ORCHESTRATOR,
                               {"agent": "demo", "label": "h1", "assume_yes": True})
        self.assertEqual(202, code)
        final = self.final(str(body["job"]), ROLE_ORCHESTRATOR)
        self.assertEqual({"bottle": "h1", "session": "h1"}, final["value"])
        self.assertEqual(sessions.ExitStatus(4), self.manager.wait_exit("h1", timeout=15))
        self.assertEqual((200, {"running": False, "exit": {"status": 4, "signal": ""}}),
                         self.call("GET", f"{SESSIONS_PATH}/h1"))

        self.backend.bottle.script = "sleep 30"
        self.backend.torn_down.clear()
        _, body = self.call("POST", BOTTLES_PATH, ROLE_CLI,
                            {"agent": "demo", "label": "h2", "assume_yes": True,
                             "cwd": "/tmp", "secrets": {"T": "v"}})
        self.assertEqual("result", self.final(str(body["job"]))["type"])
        self.assertEqual(["h2"], [b["id"] for b in self.call("GET", BOTTLES_PATH)[1]["bottles"]])
        self.assertTrue(self.call("GET", f"{SESSIONS_PATH}/h2")[1]["running"])
        code, body = self.call("DELETE", f"{BOTTLES_PATH}/h2", ROLE_ORCHESTRATOR)
        self.assertEqual(202, code)
        self.assertEqual("SIGHUP", self.final(str(body["job"]), ROLE_ORCHESTRATOR)
                         ["value"]["exit"]["signal"])
        self.assertTrue(self.backend.torn_down.is_set())
        self.assertEqual([], self.call("GET", BOTTLES_PATH)[1]["bottles"])


class TestSettingsReachTheBackend(unittest.TestCase):
    def test_backend_for_gets_the_request_settings(self) -> None:
        seen: list[Settings] = []

        def backend_for(settings: Settings) -> Any:
            seen.append(settings)
            raise RuntimeError("stop here")

        service = BottleService(sessions.SessionManager({}), backend_for=backend_for)
        settings = Settings.from_json({"operation": {"backend": "docker"}})
        with self.assertRaises(RuntimeError):
            service.launch(LaunchRequest(agent="demo", settings=settings))
        self.assertEqual([settings], seen)


if __name__ == "__main__":
    unittest.main()
