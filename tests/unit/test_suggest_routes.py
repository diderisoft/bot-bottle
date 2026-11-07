"""The CLI's suggestion routes (PRD 0117): `POST /suggest` runs the flow in
the host controller with its warm architect; `POST /architect/warm` boots it."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from bot_bottle.contract.auth import ROLE_CLI
from bot_bottle.contract.hostctl import HOST_CONTROL, ROLE_ORCHESTRATOR, Settings, bearer
from bot_bottle.control.hostctl import architect_bottle, suggest_routes
from bot_bottle.control.hostctl.runner import HostControlState
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.suggest.proposal import Proposal, ProposalAdditions


def _proposal() -> Proposal:
    p = Proposal(option="reuse", bottles=["basic"], new_bottle=None,
                 additions=ProposalAdditions(), similar=[], notes=["n"])
    p.source = "architect"
    return p


class TestRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.key = "k" * 32
        self.server = HostControlServer(("127.0.0.1", 0), "b", self.key)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = HostControlState(pid=0, port=self.server.server_address[1], build_id="b").url
        architect_bottle.install(None)
        self.addCleanup(architect_bottle.install, None)

    def post(self, path: str, body: object, role: str | None = ROLE_CLI
             ) -> tuple[int, dict[str, object]]:
        headers = {"Content-Type": "application/json"}
        if role:
            headers.update(bearer(HOST_CONTROL.mint(role, self.key)))
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_suggest_runs_the_flow_as_a_job(self) -> None:
        with mock.patch("bot_bottle.suggest.flow.suggest", return_value=_proposal()) as flow:
            code, body = self.post("/suggest", {"task": "do it", "settings": Settings().to_json()})
            self.assertEqual(202, code)
            job = self.server.jobs.get(str(body["job"]), ROLE_CLI)
            assert job is not None
            list(job.follow(timeout=5))
        self.assertEqual(("do it", None), flow.call_args.args)
        self.assertFalse(flow.call_args.kwargs["rules"])
        self.assertIsNone(flow.call_args.kwargs["run"])  # no warm bottle installed here
        value = job.events[-1]["value"]
        assert isinstance(value, dict)
        self.assertEqual("architect", value["proposal"]["source"])

    def test_suggest_passes_the_conversation(self) -> None:
        turn = {"proposal": {"option": "reuse"}, "reply": "use ./main", "events": ["x"]}
        with mock.patch("bot_bottle.suggest.flow.suggest", return_value=_proposal()) as flow:
            code, body = self.post("/suggest", {"task": "do it", "turns": [turn],
                                                "settings": Settings().to_json()})
            self.assertEqual(202, code)
            job = self.server.jobs.get(str(body["job"]), ROLE_CLI)
            assert job is not None
            list(job.follow(timeout=5))
        self.assertEqual([turn], [t.to_dict() for t in flow.call_args.kwargs["turns"]])

    def test_suggest_rejects_bad_requests_and_other_roles(self) -> None:
        good = {"task": "t", "settings": Settings().to_json()}
        self.assertEqual(401, self.post("/suggest", good, role=None)[0])
        self.assertEqual(403, self.post("/suggest", good, role=ROLE_ORCHESTRATOR)[0])
        for bad in ({"task": "", "settings": {}}, {"task": "t", "rules": "yes"},
                    {"task": "t", "cwd": 5}, {"task": "t", "turns": "hi"},
                    {"task": "t", "turns": [{"proposal": {}, "reply": 5}]}):
            with self.subTest(bad=bad):
                self.assertEqual(400, self.post("/suggest", bad)[0])

    def test_warm_boots_the_installed_bottle(self) -> None:
        self.assertEqual((202, {"state": "unavailable"}),
                         self.post("/architect/warm", {"settings": Settings().to_json()}))
        warm = mock.Mock()
        warm.status.return_value = {"state": "booting"}
        architect_bottle.install(warm)
        self.assertEqual((202, {"state": "booting"}),
                         self.post("/architect/warm", {"settings": Settings().to_json()}))
        warm.warm.assert_called_once()
        self.assertEqual(403, self.post("/architect/warm", {}, role=ROLE_ORCHESTRATOR)[0])
        self.assertEqual(400, self.post("/architect/warm", {"settings": {"bogus": {}}})[0])


class TestCwdOf(unittest.TestCase):
    def test_a_project_under_home_or_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / "p").mkdir()
            with mock.patch.object(Path, "home", return_value=home):
                self.assertEqual(home / "p", suggest_routes.cwd_of(str(home / "p")))
                self.assertIsNone(suggest_routes.cwd_of(str(home)))
                self.assertIsNone(suggest_routes.cwd_of("/somewhere/else"))
                self.assertIsNone(suggest_routes.cwd_of(""))
                self.assertIsNone(suggest_routes.cwd_of(None))
                with self.assertRaises(ValueError):
                    suggest_routes.cwd_of(5)

    def test_suggest_passes_the_installed_runner(self) -> None:
        warm = mock.Mock()
        architect_bottle.install(warm)
        self.addCleanup(architect_bottle.install, None)
        with mock.patch("bot_bottle.suggest.flow.suggest", return_value=_proposal()) as flow:
            out = suggest_routes.suggest("t", None, True, Settings())
        self.assertTrue(flow.call_args.kwargs["rules"])
        flow.call_args.kwargs["run"]("arch", "prompt")
        warm.run.assert_called_once_with("arch", "prompt", Settings())
        self.assertEqual("reuse", out["proposal"]["option"])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
