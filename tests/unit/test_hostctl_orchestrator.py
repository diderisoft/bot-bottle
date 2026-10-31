"""Unit: orchestrator bring-up through the host controller (PRD 0092 chunk 3)."""

from __future__ import annotations

import dataclasses
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from bot_bottle.contract.auth import ROLE_CLI
from bot_bottle.contract.hostctl import HOST_CONTROL, ROLE_ORCHESTRATOR, Settings, bearer
from bot_bottle.control.hostctl import orchestrator, runner
from bot_bottle.control.hostctl.runner import HostControlState
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.lib import log


class _Daemon(unittest.TestCase):
    def setUp(self) -> None:
        self.key = "k" * 32
        self.server = HostControlServer(("127.0.0.1", 0), "b", self.key)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.state = HostControlState(pid=0, port=self.server.server_address[1], build_id="b")

    def post(self, path: str, body: object, role: str | None = ROLE_CLI) -> tuple[int, dict]:  # type: ignore[type-arg]
        headers = {"Content-Type": "application/json"}
        if role:
            headers.update(bearer(HOST_CONTROL.mint(role, self.key)))
        req = urllib.request.Request(
            self.state.url + path, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestRoutes(_Daemon):
    def test_rejects_missing_token_other_roles_bad_settings_and_unknown_actions(self) -> None:
        good = {"settings": Settings().to_json()}
        self.assertEqual(401, self.post("/orchestrator/ensure", good, role=None)[0])
        self.assertEqual(403, self.post("/orchestrator/ensure", good, role=ROLE_ORCHESTRATOR)[0])
        self.assertEqual(400, self.post("/orchestrator/ensure", {"settings": {"bogus": {}}})[0])
        self.assertEqual(400, self.post("/orchestrator/ensure", [1, 2])[0])
        self.assertEqual(404, self.post("/orchestrator/nope", good)[0])

    def test_ensure_runs_as_a_job_with_the_requested_settings(self) -> None:
        settings = dataclasses.replace(
            Settings(), operation=dataclasses.replace(Settings().operation, backend="docker"))
        with mock.patch.object(orchestrator, "ensure", return_value={"url": "http://o"}) as ensure:
            code, body = self.post("/orchestrator/ensure", {"settings": settings.to_json()})
            self.assertEqual(202, code)
            job = self.server.jobs.get(str(body["job"]), ROLE_CLI)
            assert job is not None
            list(job.follow(timeout=5))
        self.assertEqual(settings, ensure.call_args.args[0])
        self.assertFalse(ensure.call_args.kwargs["restart"])
        self.assertEqual({"url": "http://o"}, job.events[-1]["value"])

    def test_restart_and_status(self) -> None:
        with mock.patch.object(orchestrator, "ensure", return_value={}) as ensure:
            _, body = self.post("/orchestrator/restart", {"settings": Settings().to_json()})
            job = self.server.jobs.get(str(body["job"]), ROLE_CLI)
            assert job is not None
            list(job.follow(timeout=5))
        self.assertTrue(ensure.call_args.kwargs["restart"])
        status = {"backend": "docker", "running": True, "url": "http://o"}
        with mock.patch.object(orchestrator, "status", return_value=status):
            self.assertEqual((200, status), self.post("/orchestrator/status", {"settings": Settings().to_json()}))


class TestEnsureViaDaemon(_Daemon):
    def setUp(self) -> None:
        super().setUp()
        patches = [
            mock.patch.object(runner, "ensure_running", return_value=self.state),
            mock.patch.object(runner, "cli_token", return_value=HOST_CONTROL.mint(ROLE_CLI, self.key)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_streams_output_and_returns_the_url_for_the_named_backend(self) -> None:
        def ensure(settings: Settings, *, restart: bool = False) -> dict[str, object]:
            del restart
            log.info(f"bringing up {settings.operation.backend}")
            return {"backend": settings.operation.backend, "url": "http://orch:8099"}

        lines: list[str] = []
        with mock.patch.object(orchestrator, "ensure", side_effect=ensure), log.redirect(lines.append):
            url = orchestrator.ensure_via_daemon(Settings(), "firecracker")
        self.assertEqual("http://orch:8099", url)
        self.assertEqual(["bot-bottle: bringing up firecracker"], lines)

    def test_a_failed_bring_up_dies_with_its_message(self) -> None:
        def ensure(settings: Settings, *, restart: bool = False) -> dict[str, object]:
            del settings, restart
            log.die("orchestrator did not become healthy")

        lines: list[str] = []
        with mock.patch.object(orchestrator, "ensure", side_effect=ensure), \
             log.redirect(lines.append), self.assertRaises(log.Die) as cm:
            orchestrator.ensure_via_daemon(Settings(), "docker")
        self.assertEqual("orchestrator did not become healthy", cm.exception.message)
        # Once: the job's `die` showed it, so the CLI doesn't repeat it.
        self.assertEqual(1, sum("did not become healthy" in line for line in lines))

    def test_detaching_points_at_the_jobs_command(self) -> None:
        with mock.patch("bot_bottle.control.hostctl.client.HostControlClient.follow",
                        return_value=None), \
             mock.patch.object(orchestrator, "ensure", return_value={}), \
             self.assertRaises(log.Die) as cm:
            orchestrator.ensure_via_daemon(Settings(), "docker")
        self.assertIn("see `bot-bottle jobs`", cm.exception.message)


class TestDaemonSide(unittest.TestCase):
    def _backend(self) -> mock.MagicMock:
        backend = mock.MagicMock()
        backend.name = "docker"
        backend.infra_service.return_value.ensure_running.return_value = "http://o"
        backend.infra_service.return_value.is_healthy.return_value = False
        return backend

    def test_ensure_restart_and_status(self) -> None:
        backend = self._backend()
        with mock.patch("bot_bottle.backend.get_bottle_backend", return_value=backend) as get:
            self.assertEqual({"backend": "docker", "url": "http://o"}, orchestrator.ensure(Settings()))
            backend.infra_service.return_value.stop.assert_not_called()
            # An adopted orchestrator still gets its listener.
            backend.infra_service.return_value.orchestrator.return_value \
                .serve_host_control.assert_called_once_with()
            orchestrator.ensure(Settings(), restart=True)
            backend.infra_service.return_value.stop.assert_called_once()
            self.assertEqual({"backend": "docker", "running": False, "url": ""},
                             orchestrator.status(Settings()))
        self.assertFalse(get.call_args.kwargs["prompt"])


if __name__ == "__main__":
    unittest.main()
