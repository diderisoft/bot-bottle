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


LAUNCH_POLICY = """routes:
  - host: "api.example.com"
    inspect:
      auth_scheme: "Bearer"
      token_env: "EGRESS_TOKEN_0"
"""
LIVE_POLICY = LAUNCH_POLICY + """  - host: "docs.example.com"
  - host: "keyed.example.com"
    inspect:
      auth_scheme: "Bearer"
      token_env: "EGRESS_TOKEN_1"
"""


class TestRunningBottle(TestAgentManifest):
    def setUp(self) -> None:
        super().setUp()
        from bot_bottle.bottle_state import egress_state_dir  # pylint: disable=import-outside-toplevel
        egress = egress_state_dir("s1")
        egress.mkdir(parents=True, exist_ok=True)
        (egress / "routes.yaml").write_text(LAUNCH_POLICY)
        project = mock.patch("bot_bottle.approvals.session_project", return_value=None)
        project.start()
        self.addCleanup(project.stop)

    def grant(self, name: str, grant: str) -> None:
        from bot_bottle.git_gate.provision import (  # pylint: disable=import-outside-toplevel
            DeployKeyRef, record_live_grant)
        stage = git_gate_state_dir("s1")
        stage.mkdir(parents=True, exist_ok=True)
        record_live_grant(stage, DeployKeyRef(name, "T", "", "h", f"o/{name}.git", grant=grant))

    def test_this_runs_changes_against_the_file(self) -> None:
        self.grant("app", "gitea.example.com/o/app:write")
        self.grant("old", "")
        with mock.patch("bot_bottle.control.hostctl.repo_grants.find_forge",
                        return_value=None):
            r = manifest_view.running_bottle("s1", LIVE_POLICY)
        assert r is not None
        self.assertEqual(("work", "~/.bot-bottle/bottles/work.md", False),
                         (r["target"], r["path"], r["project"]))
        self.assertEqual([{"kind": "egress", "what": "docs.example.com"},
                          {"kind": "repo", "what": "gitea.example.com/o/app:write"}],
                         r["changes"])
        self.assertEqual(["keyed.example.com", "old"], [u["what"] for u in r["unkept"]])
        self.assertIn("host: docs.example.com", r["updated"])
        self.assertIn("gitea.example.com/o/app:write", r["updated"])
        self.assertNotIn("keyed.example.com", r["updated"])
        self.assertIn("SECRET_ISH", r["original"])

    def test_nothing_to_keep(self) -> None:
        r = manifest_view.running_bottle("s1", LAUNCH_POLICY)
        assert r is not None
        self.assertEqual(([], r["original"]), (r["changes"], r["updated"]))
        self.assertIsNotNone(manifest_view.running_bottle("s1", None))

    def test_save_checks_what_was_shown(self) -> None:
        from bot_bottle.manifest.versions import history  # pylint: disable=import-outside-toplevel
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY):
            shown = manifest_view.running_bottle("s1", LIVE_POLICY)
            assert shown is not None
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "changed since"):
                manifest_view.save_running("s1", "stale")
            self.assertEqual("work", manifest_view.save_running("s1", shown["digest"]))
            work = Path(os.environ["BOT_BOTTLE_ROOT"]) / "bottles" / "work.md"
            self.assertIn("docs.example.com", work.read_text())
            self.assertTrue(history(work, root=Path(os.environ["BOT_BOTTLE_ROOT"]))[0]
                            .message.startswith("keep from the console: docs.example.com"))
            after = manifest_view.running_bottle("s1", LIVE_POLICY)
            assert after is not None
            self.assertEqual([], after["changes"])  # kept now
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "nothing to keep"):
                manifest_view.save_running("s1", after["digest"])

    def test_keep_target_falls_back_to_the_agents_bottle(self) -> None:
        from bot_bottle.approvals import keep_target  # pylint: disable=import-outside-toplevel
        write_metadata(BottleMetadata(identity="s3", agent_name="helper", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=()))
        target = keep_target("s3")
        assert target is not None
        self.assertEqual("work", target.label())


class TestManifestSaveRequest(unittest.TestCase):
    def ask(self, running: dict[str, str], digest: str = "d") -> dict[str, object]:
        return asyncio.run(remote_actions.manifest_save(
            {"request_id": "q", "agent_id": "a1", "digest": digest}, running))

    def test_reply(self) -> None:
        with mock.patch.object(manifest_view, "save_running", return_value="work") as save:
            reply = self.ask({"a1": "s1"})
        save.assert_called_once_with("s1", "d")
        self.assertEqual(("manifest_saved", "work"), (reply["type"], reply["saved"]))

    def test_errors(self) -> None:
        self.assertIn("isn't running", str(self.ask({})["error"]))
        with mock.patch.object(manifest_view, "save_running",
                               side_effect=manifest_view.ManifestViewError("changed")):
            self.assertEqual("changed", self.ask({"a1": "s1"})["error"])
        with mock.patch.object(manifest_view, "save_running", side_effect=OSError("ro")):
            self.assertIn("host controller's log", str(self.ask({"a1": "s1"})["error"]))
