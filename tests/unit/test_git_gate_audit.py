"""Unit: git-gate's push events, from the hook through to the orchestrator."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.contract.audit import AUDIT_EVENTS_PATH
from bot_bottle.control.git_gate_render import git_gate_render_hook
from bot_bottle.gateway.git_gate import audit

ROOT = Path(__file__).resolve().parents[2]


class _Orchestrator(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.received: list[dict[str, Any]] = []
        self.delivery = "best_effort"
        self.append_status = 200


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # pylint: disable=invalid-name
        server: Any = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        status, reply = 200, {}
        if self.path == AUDIT_EVENTS_PATH:
            server.received.append(body)
            status = server.append_status
        elif self.path == "/resolve":
            reply = {"bottle_id": "b", "audit_delivery": server.delivery}
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        del format, args


class BuildEvent(unittest.TestCase):
    def event(self, findings: int | None, outcome: str = "blocked") -> Any:
        return audit.build_event("forge.push_rejected", outcome, "refs/heads/main",
                                 bottle="amber-fox", src_ip="10.243.0.9", repo="app",
                                 findings=findings, detail="", now=0.0)

    def test_the_gitleaks_result_follows_the_findings(self) -> None:
        for findings, result in ((None, "not_scanned"), (0, "clean"), (3, "findings")):
            with self.subTest(findings=findings):
                assert (payload := self.event(findings).payload) is not None
                self.assertEqual(result, payload["gitleaks_result"])

    def test_it_names_the_bottle_repo_and_ref(self) -> None:
        event = self.event(2)
        self.assertEqual(("amber-fox", "app:refs/heads/main", "git.push"),
                         (event.bottle, event.resource, event.action))

    def test_a_report_is_counted_not_copied(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"Secret": "REDACTED"}, {"Secret": "REDACTED"}], f)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(2, audit._findings(f.name))  # pylint: disable=protected-access

    def test_the_repository_is_the_bare_repo_name(self) -> None:
        self.assertEqual("app", audit.repository(Path("/srv/gate/amber-fox/app.git")))


class HookSendsEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Orchestrator()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_hook(self, stdin: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "app.git"
            repo.mkdir()
            hook = Path(tmp) / "pre-receive"
            hook.write_text(git_gate_render_hook())
            url = f"http://127.0.0.1:{self.server.server_address[1]}"
            python_dir = Path(sys.executable).parent
            env = {**os.environ, "BOT_BOTTLE_ORCHESTRATOR_URL": url,
                   "GIT_GATE_BOTTLE": "amber-fox", "SUPERVISE_SOURCE_IP": "10.243.0.9",
                   "PYTHONPATH": str(ROOT), "PATH": f"{python_dir}:{os.environ['PATH']}"}
            return subprocess.run(["sh", str(hook)], input=stdin, capture_output=True, text=True,
                                  cwd=repo, env=env, check=False, timeout=60)

    def test_a_refused_agit_ref_is_recorded(self) -> None:
        result = self.run_hook(f"{'0' * 40} {'1' * 40} refs/for/main\n")
        self.assertEqual(1, result.returncode)
        [body] = self.server.received  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual("git-gate", body["producer"])
        event = body["event"]
        self.assertEqual(("forge.push_rejected", "blocked", "amber-fox"),
                         (event["type"], event["outcome"], event["bottle"]))
        self.assertEqual({"repository": "app", "ref": "refs/for/main", "detail": "AGit review ref"},
                         {k: event["payload"][k] for k in ("repository", "ref", "detail")})



class RequiredDelivery(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Orchestrator()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        env = mock.patch.dict(os.environ, {
            "BOT_BOTTLE_ORCHESTRATOR_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
            "SUPERVISE_SOURCE_IP": "10.243.0.9"})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = audit.main(list(args))
        return rc, out.getvalue().strip()

    def test_delivery_prints_the_bottles_mode(self) -> None:
        self.assertEqual((0, "best_effort"), self.run_main("delivery"))
        self.server.delivery = "required"
        self.assertEqual((0, "required"), self.run_main("delivery"))

    def test_an_unresolvable_bottle_is_treated_as_required(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual((0, "required"), self.run_main("delivery"))

    def test_required_fails_unless_written(self) -> None:
        args = ("--required", "forge.push_accepted", "success", "refs/heads/main")
        self.assertEqual(0, self.run_main(*args)[0])
        self.assertTrue(self.server.received[-1]["required"])
        self.server.append_status = 503
        self.assertEqual(1, self.run_main(*args)[0])

    def test_best_effort_never_fails_the_push(self) -> None:
        self.server.append_status = 503
        self.assertEqual(0, self.run_main("forge.push_accepted", "success", "refs/heads/main")[0])

if __name__ == "__main__":
    unittest.main()
