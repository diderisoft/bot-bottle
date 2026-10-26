"""A running agent's manifest for the console's Manifest tab."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.bottle_state import BottleMetadata, git_gate_state_dir, write_metadata
from bot_bottle.control.hostctl import manifest_view, remote_actions

AGENT = """---
bottle: work
agent_provider:
  harness: pi
---

Be helpful.
"""
WORK = """---
extends: base
env:
  SECRET_ISH: literal-value
egress:
  routes:
    - host: example.com
      inspect:
        matches:
          - methods: [GET]
            paths:
              - value: /v1/
---

Work bottle.
"""
BASE = """---
env:
  LANG: C.UTF-8
---

Base.
"""
POLICY = """routes:
  - host: "example.com"
  - host: "api.example.com"
    inspect:
      auth_scheme: "Bearer"
      token_env: "EGRESS_TOKEN_0"
"""


class TestAgentManifest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        root = home / ".bot-bottle"
        (root / "agents").mkdir(parents=True)
        (root / "bottles").mkdir()
        (root / "agents" / "helper.md").write_text(AGENT)
        (root / "bottles" / "work.md").write_text(WORK)
        (root / "bottles" / "base.md").write_text(BASE)
        env = mock.patch.dict(os.environ, {"HOME": str(home), "BOT_BOTTLE_ROOT": str(root)})
        env.start()
        self.addCleanup(env.stop)
        write_metadata(BottleMetadata(identity="s1", agent_name="helper", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=("work",)))

    def test_files_and_what_they_resolve_to(self) -> None:
        with mock.patch.object(manifest_view, "_live_policy", return_value=None):
            m = manifest_view.agent_manifest("s1")
        self.assertEqual(("helper", ["work"], "pi"), (m["agent"], m["bottles"], m["harness"]))
        self.assertEqual([("agent", "helper"), ("bottle", "work"), ("bottle", "base")],
                         [(f["kind"], f["name"]) for f in m["files"]])
        self.assertIn("Be helpful.", m["files"][0]["text"])
        self.assertEqual("~/.bot-bottle/agents/helper.md", m["files"][0]["path"])
        r = m["resolved"]
        self.assertEqual(["LANG", "SECRET_ISH"], r["env"])  # names only
        self.assertNotIn("literal-value", repr(r))
        self.assertFalse(r["egress_live"])
        self.assertEqual([{"host": "example.com", "methods": ["GET"], "paths": ["/v1/"],
                           "credential": False, "git_fetch": False, "declared": True}],
                         r["egress"])

    def test_live_egress_and_live_repos(self) -> None:
        from bot_bottle.git_gate.provision import (  # pylint: disable=import-outside-toplevel
            DeployKeyRef, _write_upstreams_snapshot, record_live_grant)
        from bot_bottle.control.git_gate_render import (  # pylint: disable=import-outside-toplevel
            GitGateUpstream)
        stage = git_gate_state_dir("s1")
        stage.mkdir(parents=True)
        record_live_grant(stage, DeployKeyRef("app", "T", "", "h", "o/app.git"))
        _write_upstreams_snapshot(stage, (GitGateUpstream(
            name="app", upstream_url="ssh://git@h/o/app.git", upstream_host="h",
            upstream_port="", identity_file="", known_host_key="", writable=True),))
        with mock.patch.object(manifest_view, "_live_policy", return_value=POLICY):
            r = manifest_view.agent_manifest("s1")["resolved"]
        self.assertTrue(r["egress_live"])
        self.assertEqual([("example.com", False, True), ("api.example.com", True, False)],
                         [(e["host"], e["credential"], e["declared"]) for e in r["egress"]])
        self.assertEqual([{"name": "app", "url": "ssh://git@h/o/app.git", "writable": True,
                           "live": True}], r["repos"])

    def test_errors(self) -> None:
        with self.assertRaisesRegex(manifest_view.ManifestViewError, "launch record"):
            manifest_view.agent_manifest("nope")
        write_metadata(BottleMetadata(identity="s2", agent_name="gone", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=("work",)))
        with self.assertRaisesRegex(manifest_view.ManifestViewError, "doesn't load"):
            manifest_view.agent_manifest("s2")

    def test_live_policy_from_the_orchestrator(self) -> None:
        client = mock.Mock()
        client.list_bottles.return_value = [{"bottle_id": "other", "policy": "x"},
                                            {"bottle_id": "reg-1", "policy": POLICY}]
        with mock.patch("bot_bottle.backend.adoption.read",
                        return_value=mock.Mock(registry_id="reg-1")), \
                mock.patch("bot_bottle.control.client.discover_orchestrator_url",
                           return_value="http://o"), \
                mock.patch("bot_bottle.control.client.OrchestratorClient",
                           return_value=client):
            self.assertEqual(POLICY, manifest_view._live_policy("s1"))  # pylint: disable=protected-access
        with mock.patch("bot_bottle.backend.adoption.read", return_value=None):
            self.assertIsNone(manifest_view._live_policy("s1"))  # pylint: disable=protected-access


class TestManifestRequest(unittest.TestCase):
    def ask(self, running: dict[str, str]) -> dict[str, object]:
        return asyncio.run(remote_actions.manifest(
            {"type": "manifest", "request_id": "q", "agent_id": "a1"}, running))

    def test_reply(self) -> None:
        with mock.patch.object(manifest_view, "agent_manifest",
                               return_value={"agent": "helper"}) as view:
            reply = self.ask({"a1": "s1"})
        view.assert_called_once_with("s1")
        self.assertEqual(("manifest", "q", "helper"),
                         (reply["type"], reply["request_id"], reply["agent"]))

    def test_errors(self) -> None:
        self.assertIn("isn't running", str(self.ask({})["error"]))
        with mock.patch.object(manifest_view, "agent_manifest",
                               side_effect=manifest_view.ManifestViewError("gone")):
            self.assertEqual("gone", self.ask({"a1": "s1"})["error"])
        with mock.patch.object(manifest_view, "agent_manifest", side_effect=OSError("x")):
            self.assertIn("host controller's log", str(self.ask({"a1": "s1"})["error"]))


if __name__ == "__main__":
    unittest.main()
