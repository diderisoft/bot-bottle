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
from bot_bottle.lib import log
from bot_bottle.contract.auth import ROLE_CLI, ROLE_GATEWAY
from bot_bottle.contract.hostctl import (
    AUDIT_APPEND_PATH, BOTTLES_PATH, CONFIRM_ERROR, HOST_CONTROL, LAUNCH_CLI_ONLY,
    ROLE_ORCHESTRATOR, SESSIONS_PATH, InfraSettings, LaunchRequest, Settings, bearer, exit_code,
)
from bot_bottle.control.hostctl import localsock, sessions
from bot_bottle.control.hostctl.audit_events import HostAudit
from bot_bottle.control.hostctl.audit_journal import AuditJournal
from bot_bottle.control.hostctl.bottles import BottleService, ConfirmationNeeded
from bot_bottle.control.hostctl.jobs import JobRegistry
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.image_cache import StaleImageError
from bot_bottle.lib import log
from bot_bottle.lib.log import Die

from . import use_bottle_root

_TMUX_VERSION = sessions.tmux_version() if shutil.which("tmux") else None
_TMUX_OK = _TMUX_VERSION is not None and _TMUX_VERSION >= sessions.MIN_TMUX_VERSION




def _without_last_output(state: Any) -> Any:
    """A session state with its exit's `last_output` (the failed pane's last
    lines, whatever the fake agent printed) checked and dropped."""
    ended = dict(state["exit"])
    assert isinstance(ended.pop("last_output"), str)
    return {**state, "exit": ended}

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
            value = {"cwd": "/w", "copy_cwd": False, "secrets": {},
                     "host_keys": {}, "run_bottle": "---\n---\n"}[name]
            with self.subTest(name), self.assertRaises(PermissionError):
                LaunchRequest.from_json({"agent": "demo", name: value}, ROLE_ORCHESTRATOR)
        LaunchRequest.from_json({"agent": "demo", "prompt": "hi"}, ROLE_ORCHESTRATOR)

    def test_copy_needs_a_cwd_and_there_is_no_mount(self) -> None:
        with self.assertRaises(ValueError):
            LaunchRequest(agent="demo", copy_cwd=True).check()
        with self.assertRaises(ValueError):
            LaunchRequest.from_json({"agent": "demo", "cwd": "/w", "mount_cwd": True}, ROLE_CLI)

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
    registry_id = ""

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
        self.launch_warning = ""
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
            slug=slug, harness="claude",
            agent_provision=SimpleNamespace(startup_args=("--start",)),
            manifest=SimpleNamespace(agent=SimpleNamespace(remote_writable=True),
                                     bottle=SimpleNamespace(git=())),
            workspace_plan=SimpleNamespace(enabled=False, has_host_git_dir=False, mount=False,
                                           guest_path=""),
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
        if self.launch_warning:
            log.warn(self.launch_warning)
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
        # The fake plan's bottle isn't a real one to record a version of.
        self.enterContext(mock.patch("bot_bottle.run_manifest.record_launch"))
        self.manager = sessions.SessionManager({})
        self.backend = FakeBackend()
        self.index = mock.MagicMock()
        self.journal = AuditJournal(root / "audit", host="h", engine="e")
        self.addCleanup(self.journal.close)
        self.service = BottleService(self.manager, backend_for=lambda _s: cast(Any, self.backend),
                                     manifest_for=lambda _cwd: self.index,
                                     secrets_file=root / "secrets",
                                     audit=HostAudit(self.journal))

    def audited(self) -> list[dict[str, Any]]:
        """Every journal record after the segment's opening one."""
        lines = self.journal.path.read_text().splitlines()[1:]
        return [json.loads(line) for line in lines]

    def types(self) -> list[str]:
        return [r["type"] for r in self.audited()]


class TestLaunchConfirmation(_Root):
    """Everything before the holder: no tmux needed."""

    def test_a_launch_is_refused_once_the_code_on_disk_changed(self) -> None:
        # #679: lazy imports would mix old classes with new modules.
        service = BottleService(self.manager, backend_for=lambda _s: cast(Any, self.backend),
                                manifest_for=lambda _cwd: self.index, code_changed=lambda: True)
        with self.assertRaises(log.Die) as cm:
            service.launch(LaunchRequest(agent="demo", label="c0", cwd="/w"))
        self.assertIn("up --restart", cm.exception.message)
        self.assertEqual([], self.backend.specs)

    def test_only_a_stale_image_asks_and_without_a_preflight(self) -> None:
        # A launch doesn't ask to go ahead; a stale image still does, and an
        # interactive launch gets no preflight summary (only headless logs one).
        self.backend.stale = True
        lines: list[str] = []
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value="agent:"), \
             mock.patch("bot_bottle.lib.log.current_sink", return_value=lines.append), \
             mock.patch("bot_bottle.lib.log.write_line", side_effect=lines.append):
            with self.assertRaises(ConfirmationNeeded) as cm:
                self.service.launch(LaunchRequest(agent="demo", label="c1", cwd="/w"))
        self.assertIn("Launch anyway?", str(cm.exception))
        self.assertNotIn("backend: fake", lines)
        self.assertFalse(self.backend.launched.is_set())
        # The aborted launch's state dir is reaped.
        self.assertFalse(bottle_state.bottle_state_dir("c1").exists())

    def test_the_spec_is_non_interactive_and_secrets_come_from_the_request(self) -> None:
        self.backend.stale = True  # stops it at the yes/no, after preparing
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
        self.backend.stale = True
        jobs = JobRegistry()
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""):
            job = jobs.start("bottle.launch", ROLE_CLI,
                             lambda: self.service.launch(LaunchRequest(agent="demo", label="j1")))
            final = list(job.follow(timeout=10))[-1]
        self.assertEqual(("error", CONFIRM_ERROR), (final["type"], final.get("code")))
        self.assertIn("Launch anyway?", str(final["message"]))

    def test_a_failed_launch_is_the_jobs_error(self) -> None:
        self.backend.fail_launch = True
        with self.assertRaises(RuntimeError):
            self.service.launch(LaunchRequest(agent="demo", label="f1", assume_yes=True))
        self.assertFalse(bottle_state.bottle_state_dir("f1").exists())
        self.assertEqual([], self.service.list())


class TestLaunchAudit(_Root):
    def test_a_launch_waiting_for_confirmation_records_nothing(self) -> None:
        self.backend.stale = True  # the one yes/no left
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""), \
             self.assertRaises(ConfirmationNeeded):
            self.service.launch(LaunchRequest(agent="demo", label="a1"))
        self.assertEqual([], self.types())

    def test_a_failed_launch_is_a_failed_broker_launch(self) -> None:
        self.backend.fail_launch = True
        with self.assertRaises(RuntimeError):
            self.service.launch(LaunchRequest(agent="demo", label="a2", assume_yes=True,
                                              secrets={"TOKEN": "not-a-real-secret-value"}))
        [record] = self.audited()  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual(("hostctl.broker_launch", "failure", "role:cli"),
                         (record["type"], record["outcome"], record["actor"]))
        self.assertIn("no network", record["payload"]["detail"])
        self.assertNotIn("not-a-real-secret-value", self.journal.path.read_text())

@unittest.skipUnless(_TMUX_OK, "needs tmux 3.1+")
class TestHeldBottles(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.local = localsock.serve(sessions.control_socket_path(), self.manager)
        self.addCleanup(self.local.close)

    def launch(self, label: str) -> dict[str, object]:
        return self.service.launch(LaunchRequest(agent="demo", label=label, assume_yes=True))

    def test_a_launch_goes_ahead_without_asking(self) -> None:
        launched = self.service.launch(LaunchRequest(agent="demo", label="n1"))
        self.assertEqual("n1", launched["session"])
        self.manager.wait_exit("n1", timeout=15)

    def test_agent_exit_tears_down_and_reports_its_code(self) -> None:
        launched = self.launch("b1")
        # The run's activation id rides along (the console scopes its audit query by it).
        self.assertTrue(launched.pop("activation"))
        self.assertEqual({"bottle": "b1", "session": "b1"}, launched)
        self.assertEqual(sessions.ExitStatus(4), self.manager.wait_exit("b1", timeout=15))
        # The exit is recorded only after the holder tore the bottle down.
        self.assertTrue(self.backend.torn_down.is_set())
        self.assertEqual({"running": False, "exit": {"status": 4, "signal": ""}},
                         _without_last_output(self.service.session_state("b1")))
        self.assertEqual("--start", self.backend.bottle.argv[-1])
        # claude exited non-zero, so its state is kept for `resume`.
        self.assertTrue(bottle_state.is_preserved("b1"))
        self.assertEqual([], self.service.list())

    def test_a_console_launch_is_listed_with_its_origin(self) -> None:
        from bot_bottle.control.hostctl.remote import ROLE_REMOTE

        self.backend.bottle.script = "echo up; sleep 30"
        self.service.launch(LaunchRequest(agent="demo", label="c1", assume_yes=True,
                                          agent_id="console-id"), ROLE_REMOTE)
        self.assertEqual([("console", "console-id")],
                         [(b["origin"], b["agent_id"]) for b in self.service.list()])
        self.service.teardown("c1")

    def _stopped(self, identity: str, origin: str = "", agent_id: str = "") -> None:
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=identity, agent_name="demo", cwd="", copy_cwd=False, started_at="",
            origin=origin, agent_id=agent_id))

    def _resume(self, identity: str, role: str = ROLE_CLI) -> list[tuple[object, object]]:
        self.backend.bottle.script = "echo up; sleep 30"
        self.service.launch(LaunchRequest(agent="demo", label=identity, identity=identity,
                                          assume_yes=True), role)
        try:
            return [(b["origin"], b["agent_id"]) for b in self.service.list()]
        finally:
            self.service.teardown(identity)

    def test_a_cli_resume_of_a_console_bottle_comes_back_as_its_console_agent(self) -> None:
        self._stopped("r1", origin="console", agent_id="console-id")
        self.assertEqual([("console", "console-id")], self._resume("r1"))
        meta = bottle_state.read_metadata("r1")
        assert meta is not None
        self.assertEqual(("console", "console-id"), (meta.origin, meta.agent_id))

    def test_a_cli_resume_of_a_terminal_or_older_bottle_stays_a_terminal_one(self) -> None:
        for identity, origin in (("t1", "terminal"), ("o1", "")):
            with self.subTest(identity=identity):
                self._stopped(identity, origin)
                self.assertEqual([("terminal", "")], self._resume(identity))
                meta = bottle_state.read_metadata(identity)
                assert meta is not None
                self.assertEqual(("terminal", ""), (meta.origin, meta.agent_id))

    def test_a_console_launch_records_who_started_it(self) -> None:
        from bot_bottle.control.hostctl.remote import ROLE_REMOTE

        self._stopped("c2")
        self.backend.bottle.script = "echo up; sleep 30"
        self.service.launch(LaunchRequest(agent="demo", label="c2", identity="c2",
                                          assume_yes=True, agent_id="cid"), ROLE_REMOTE)
        self.service.teardown("c2")
        meta = bottle_state.read_metadata("c2")
        assert meta is not None
        self.assertEqual(("console", "cid"), (meta.origin, meta.agent_id))

    def test_a_launch_warning_reaches_the_sessions_status_bar(self) -> None:
        self.backend.bottle.script = "echo up; sleep 30"
        self.backend.launch_warning = ("isolation gap: agent can reach the host at 10.0.0.1; "
                                       "public addresses and DNS are blocked.")
        self.launch("w1")
        shown = subprocess.run(sessions.tmux("show-options", "-v", "-t", "=w1:", "@bb_warning"),
                               capture_output=True, text=True, check=False).stdout.strip()
        self.assertEqual("isolation gap: agent can reach the host at 10.0.0.1", shown)
        self.service.teardown("w1")

    def test_list_and_delete(self) -> None:
        self.backend.bottle.script = "echo up; sleep 30"
        self.launch("b2")
        listed = self.service.list()
        self.assertEqual([("b2", "fake", "demo", True, True)],
                         [(b["id"], b["backend"], b["agent"], b["held"],
                           cast(dict[str, object], b["session"])["running"]) for b in listed])
        # What the console link presents a session it didn't launch with.
        self.assertEqual(("terminal", True), (listed[0]["origin"], listed[0]["writable"]))
        self.assertTrue(listed[0]["activation"])
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
        rejects = [r for r in self.audited() if r["type"] == "hostctl.broker_reject"]
        self.assertEqual(
            [("unauthenticated", f"POST {BOTTLES_PATH}"), ("role:gateway", f"POST {BOTTLES_PATH}"),
             ("role:orchestrator", f"POST {BOTTLES_PATH}"), ("role:gateway", f"GET {BOTTLES_PATH}"),
             ("role:gateway", f"GET {SESSIONS_PATH}/x")],
            [(r["actor"], r["payload"]["op"]) for r in rejects])
        self.assertTrue(all(r["outcome"] == "blocked" for r in rejects))

    def test_audit_append(self) -> None:
        event = {"type": "auth.token_rejected", "actor": "unauthenticated",
                 "action": "GET /x", "outcome": "blocked", "sensitivity": "security",
                 "ts_event": "2026-10-23T00:00:00.000Z", "id": "e-1"}
        body = {"producer": "egress", "event": event}
        self.assertEqual((200, {"written": True}),
                         self.call("POST", AUDIT_APPEND_PATH, ROLE_ORCHESTRATOR, body))
        # A retry with the same id isn't written twice.
        self.assertEqual((200, {"written": False}),
                         self.call("POST", AUDIT_APPEND_PATH, ROLE_ORCHESTRATOR, body))
        [record] = [r for r in self.audited() if r["id"] == "e-1"]
        self.assertEqual("egress", record["producer"])
        # Only the orchestrator appends; it relays the gateway's events.
        self.assertEqual(403, self.call("POST", AUDIT_APPEND_PATH, ROLE_GATEWAY, body)[0])
        self.assertEqual(400, self.call("POST", AUDIT_APPEND_PATH, ROLE_ORCHESTRATOR,
                                        {"producer": "hostctl", "event": event})[0])
        self.assertEqual(400, self.call("POST", AUDIT_APPEND_PATH, ROLE_ORCHESTRATOR,
                                        {"producer": "egress",
                                         "event": {**event, "host": "spoofed"}})[0])
        self.assertEqual(422, self.call("POST", AUDIT_APPEND_PATH, ROLE_ORCHESTRATOR,
                                        {"producer": "egress",
                                         "event": {**event, "id": "e-2",
                                                   "payload": {"api_key": "x"}}})[0])

    def test_launch_exit_code_and_delete_over_http(self) -> None:
        code, body = self.call("POST", BOTTLES_PATH, ROLE_ORCHESTRATOR,
                               {"agent": "demo", "label": "h1", "assume_yes": True})
        self.assertEqual(202, code)
        final = self.final(str(body["job"]), ROLE_ORCHESTRATOR)
        self.assertTrue(final["value"].pop("activation"))
        self.assertEqual({"bottle": "h1", "session": "h1"}, final["value"])
        self.assertEqual(sessions.ExitStatus(4), self.manager.wait_exit("h1", timeout=15))
        code, state = self.call("GET", f"{SESSIONS_PATH}/h1")
        self.assertEqual((200, {"running": False, "exit": {"status": 4, "signal": ""}}),
                         (code, _without_last_output(state)))

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


class TestOrchestratorLaunchSettings(_Root):
    """An `orchestrator` launch runs on the backend's recorded infra settings
    and resolves its secrets from the host secrets file."""

    def setUp(self) -> None:
        super().setUp()
        # Each launch stops at the stale-image yes/no, after preparing.
        self.backend.stale = True
        self.seen: list[Settings] = []
        self.recorded: InfraSettings | None = None

        def backend_for(settings: Settings) -> Any:
            self.seen.append(settings)
            return self.backend

        self.service = BottleService(
            self.manager, backend_for=backend_for, manifest_for=lambda _cwd: self.index,
            secrets_file=self.secrets_file(), recorded_infra=lambda _name: self.recorded)

    def secrets_file(self) -> Path:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secrets"
        path.write_text("MY_TOKEN=from-file\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def launch(self, role: str) -> None:
        request = LaunchRequest(agent="demo", label="o1", settings=Settings.from_json({
            "operation": {"no_cache": True}, "infra": {"egress_port": 5555}}))
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""), \
             self.assertRaises(ConfirmationNeeded):
            self.service.launch(request, role)

    def test_recorded_infra_replaces_the_requests(self) -> None:
        self.recorded = InfraSettings(egress_port=7777, fc_pool_size=3)
        self.launch(ROLE_ORCHESTRATOR)
        used = self.seen[-1]
        self.assertEqual(self.recorded, used.infra)
        # Operation settings stay; the backend is pinned to the one resolved.
        self.assertTrue(used.operation.no_cache)
        self.assertEqual("fake", used.operation.backend)

    def test_defaults_when_nothing_is_recorded(self) -> None:
        self.launch(ROLE_ORCHESTRATOR)
        self.assertEqual(InfraSettings(), self.seen[-1].infra)

    def test_cli_launch_keeps_its_infra(self) -> None:
        self.recorded = InfraSettings(egress_port=7777)
        self.launch(ROLE_CLI)
        self.assertEqual([5555], [s.infra.egress_port for s in self.seen])

    def test_token_ref_resolves_from_the_secrets_file(self) -> None:
        from bot_bottle.egress import Egress  # pylint: disable=import-outside-toplevel
        self.launch(ROLE_ORCHESTRATOR)
        self.assertEqual({"EGRESS_TOKEN_0": "from-file"}, Egress().resolve_token_values(
            {"EGRESS_TOKEN_0": "MY_TOKEN"}, self.backend.secrets[-1]))


class TestRecordedInfraSettings(unittest.TestCase):
    def test_reads_what_bring_up_recorded(self) -> None:
        from bot_bottle.backend.infra_service import (  # pylint: disable=import-outside-toplevel
            infra_settings_record, recorded_infra_settings)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(use_bottle_root(root))
        self.assertIsNone(recorded_infra_settings("docker"))
        path = infra_settings_record("docker")
        path.parent.mkdir(parents=True)
        infra = InfraSettings(egress_port=7777)
        path.write_text(json.dumps({"infra": Settings(infra=infra).to_json()["infra"]}))
        self.assertEqual(infra, recorded_infra_settings("docker"))
        path.write_text(json.dumps({"infra": {"retired_knob": 1}}))
        self.assertIsNone(recorded_infra_settings("docker"))



@unittest.skipUnless(_TMUX_OK, "needs tmux 3.1+")
class TestLifecycleAudit(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.local = localsock.serve(sessions.control_socket_path(), self.manager)
        self.addCleanup(self.local.close)

    def launch(self, label: str) -> None:
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value="agent:"):
            self.service.launch(LaunchRequest(agent="demo", label=label, assume_yes=True))

    def test_an_agent_that_exits_non_zero_crashed(self) -> None:
        self.launch("l1")
        self.manager.wait_exit("l1", timeout=15)
        # An agent that exits at once can crash before the launch call has
        # returned and recorded itself, so only the start's place is fixed.
        types = self.types()
        self.assertEqual("lifecycle.bottled_agent_start", types[0])
        self.assertEqual(["hostctl.broker_launch", "lifecycle.bottled_agent_crash"],
                         sorted(types[1:]))
        by_type = {r["type"]: r for r in self.audited()}
        start = by_type["lifecycle.bottled_agent_start"]
        crash = by_type["lifecycle.bottled_agent_crash"]
        self.assertEqual(start["activation"], crash["activation"])
        self.assertTrue(start["manifest_digest"].startswith("sha256:"))
        self.assertEqual(start["manifest_digest"], crash["manifest_digest"])
        self.assertEqual((4, "failure"), (crash["payload"]["exit"], crash["outcome"]))

    def test_a_requested_stop_is_a_stop(self) -> None:
        self.backend.bottle.script = "sleep 30"
        self.launch("l2")
        self.service.teardown("l2")
        self.assertEqual(["lifecycle.bottled_agent_start", "hostctl.broker_launch",
                          "lifecycle.bottled_agent_stop", "hostctl.broker_teardown"],
                         self.types())
        stop = self.audited()[2]
        self.assertTrue(stop["payload"]["stop_requested"])
        self.assertEqual("success", stop["outcome"])

    def test_the_activation_is_the_orchestrators_registry_id(self) -> None:
        self.backend.bottle.registry_id = "3f9a0c1d2e4b5a69"
        self.launch("l4")
        self.manager.wait_exit("l4", timeout=15)
        activations = {r["activation"] for r in self.audited()
                       if r["type"].startswith("lifecycle.")}
        self.assertEqual({"3f9a0c1d2e4b5a69"}, activations)

    def test_each_run_is_its_own_activation(self) -> None:
        self.launch("l3")
        self.manager.wait_exit("l3", timeout=15)
        self.launch("l3")
        self.manager.wait_exit("l3", timeout=15)
        starts = [r for r in self.audited() if r["type"] == "lifecycle.bottled_agent_start"]
        self.assertEqual(2, len(starts))
        self.assertNotEqual(starts[0]["activation"], starts[1]["activation"])


class TestLogPreflight(unittest.TestCase):
    """The launch log carries the docker backend's weak-boundary note (#606)."""

    def _logged(self, plan: object, backend_name: str) -> list[str]:
        from bot_bottle.control.hostctl import bottles as bottles_mod
        warned: list[str] = []
        with mock.patch.object(bottles_mod.log, "write_line"), \
             mock.patch.object(bottles_mod.log, "warn", side_effect=warned.append), \
             mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""):
            bottles_mod._log_preflight(cast(Any, plan), backend_name)  # pyright: ignore[reportPrivateUsage]
        return warned

    def test_docker_without_gvisor_warns(self) -> None:
        warned = self._logged(SimpleNamespace(manifest=None, use_runsc=False), "docker")
        self.assertEqual(1, len(warned))
        self.assertIn("sharing the host kernel", warned[0])

    def test_vm_backends_do_not(self) -> None:
        self.assertEqual([], self._logged(SimpleNamespace(manifest=None), "firecracker"))


class TestAdoptAfterRestart(unittest.TestCase):
    """A restarted controller re-takes what running bottles hold (#679)."""

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbadopt.")))
        self.addCleanup(use_bottle_root(root))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.sessions = mock.MagicMock()
        self.records = [
            sessions.SessionRecord(id="fc1", kind="bottle", meta={"backend": "firecracker"}),
            sessions.SessionRecord(id="dead", kind="bottle", meta={"backend": "firecracker"}),
            sessions.SessionRecord(id="other", kind="console", meta={"backend": "firecracker"}),
        ]
        self.sessions.records.return_value = self.records
        def exists(session_id: str) -> bool:
            return session_id != "dead"
        self.sessions.exists.side_effect = exists
        self.release = mock.Mock()
        self.backend = mock.MagicMock()
        self.backend.hold_adopted.return_value = self.release
        self.service = BottleService(self.sessions, backend_for=lambda _s: self.backend,
                                     recorded_infra=lambda _b: None,
                                     audit=mock.MagicMock())

    def test_running_bottles_are_held_and_released_when_they_end(self) -> None:
        self.service.adopt()
        self.backend.hold_adopted.assert_called_once_with("fc1")
        self.release.assert_not_called()
        self.service._finish(self.records[0], sessions.ExitStatus(0))  # pyright: ignore[reportPrivateUsage]
        self.release.assert_called_once_with()

    def test_a_backend_that_fails_to_adopt_is_skipped(self) -> None:
        self.backend.hold_adopted.side_effect = RuntimeError("boom")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.service.adopt()  # doesn't raise
        self.assertIn("couldn't adopt", err.getvalue())


if __name__ == "__main__":
    unittest.main()
