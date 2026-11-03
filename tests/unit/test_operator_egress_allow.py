"""The operator's one-tap allow of a host a running agent was blocked from:
`manifest_view.allow_in_run` and the console's `operator_egress_allow`."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from bot_bottle.control.hostctl import manifest_view, remote_actions
from bot_bottle.gateway.egress.schema import load_config

from tests.unit import test_manifest_view as _mv

LIVE_POLICY = _mv.LIVE_POLICY


class TestAllowInRun(_mv.TestRunningBottle):
    def allow(self, host: str, *, keep: bool = False, policy: str | None = LIVE_POLICY,
              adopted: object = mock.Mock(registry_id="reg-1"),
              ) -> tuple[dict[str, object], mock.Mock]:
        client = mock.Mock()
        with mock.patch.object(manifest_view, "_live_policy", return_value=policy), \
                mock.patch("bot_bottle.backend.adoption.read", return_value=adopted), \
                mock.patch("bot_bottle.control.client.env_orchestrator_url",
                           return_value="http://o"), \
                mock.patch("bot_bottle.control.client.OrchestratorClient", return_value=client):
            return manifest_view.allow_in_run("s1", host, keep=keep), client

    def test_adds_a_default_route_to_the_running_policy(self) -> None:
        reply, client = self.allow(" New.Example.com ")
        self.assertEqual({"allowed": "new.example.com"}, reply)
        bottle_id, text = client.set_policy.call_args.args
        routes = {r.host: r for r in load_config(text).routes}
        self.assertEqual("reg-1", bottle_id)
        before = {r.host for r in load_config(LIVE_POLICY).routes}
        self.assertEqual(before | {"new.example.com"}, set(routes))
        self.assertEqual((), routes["new.example.com"].matches)
        self.assertEqual("EGRESS_TOKEN_1", routes["keyed.example.com"].token_env)  # kept as was

    def test_keep_writes_the_route_into_the_bottle(self) -> None:
        from bot_bottle.approvals import keep_target  # pylint: disable=import-outside-toplevel
        target = keep_target("s1")
        assert target is not None
        reply, _ = self.allow("new.example.com", keep=True)
        self.assertEqual(target.label(), reply["kept"])
        self.assertIn("new.example.com", target.path.read_text(encoding="utf-8"))

    def test_refusals(self) -> None:
        for host, why in (("not a host", "isn't a host name"), ("*", "isn't a host name"),
                          ("x.io/path", "isn't a host name"), ("localhost", "isn't a host name"),
                          ("docs.example.com", "already has a route")):
            with self.subTest(host=host):
                with self.assertRaisesRegex(manifest_view.ManifestViewError, why):
                    self.allow(host)
        with self.assertRaisesRegex(manifest_view.ManifestViewError, "can't reach"):
            self.allow("new.example.com", policy=None)
        with self.assertRaisesRegex(manifest_view.ManifestViewError, "can't reach"):
            self.allow("new.example.com", adopted=None)

    def test_keep_with_nowhere_to_keep_it(self) -> None:
        with mock.patch("bot_bottle.approvals.keep_target", return_value=None), \
                self.assertRaisesRegex(manifest_view.ManifestViewError, "no bottle to keep it in"):
            self.allow("new.example.com", keep=True)


class TestOperatorEgressAllowRequest(unittest.TestCase):
    def ask(self, running: dict[str, str], **msg: object) -> dict[str, object]:
        return asyncio.run(remote_actions.QUERIES["operator_egress_allow"](
            {"type": "operator_egress_allow", "request_id": "r1", "agent_id": "a1", **msg},
            running, None))

    def test_only_an_agent_running_here(self) -> None:
        reply = self.ask({}, host="x.io")
        self.assertEqual("that agent isn't running on this host", reply["error"])
        self.assertEqual(("operator_egress_allowed", "r1"), (reply["type"], reply["request_id"]))

    def test_allows_for_the_agents_bottle(self) -> None:
        with mock.patch.object(manifest_view, "allow_in_run",
                               return_value={"allowed": "x.io", "kept": "work"}) as allow:
            reply = self.ask({"a1": "s1"}, host="x.io", keep=True)
        allow.assert_called_once_with("s1", "x.io", keep=True)
        self.assertEqual(("x.io", "work"), (reply["allowed"], reply["kept"]))

    def test_errors_are_reported(self) -> None:
        with mock.patch.object(manifest_view, "allow_in_run",
                               side_effect=manifest_view.ManifestViewError("nope")):
            self.assertEqual("nope", self.ask({"a1": "s1"}, host="x.io")["error"])
        with mock.patch.object(manifest_view, "allow_in_run", side_effect=RuntimeError("boom")):
            self.assertIn("host controller's log", str(self.ask({"a1": "s1"}, host="x")["error"]))


if __name__ == "__main__":
    unittest.main()
