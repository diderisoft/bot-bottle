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

    def test_what_it_resolves_to(self) -> None:
        with mock.patch.object(manifest_view, "_live_policy", return_value=None):
            m = manifest_view.agent_manifest("s1")
        self.assertEqual(("helper", ["work"], "pi"), (m["agent"], m["bottles"], m["harness"]))
        current = m["current"]
        self.assertEqual((1, "the bottle files when first viewed", []),
                         (current["version"], current["message"], current["changes"]))
        self.assertIn("LANG: C.UTF-8", current["text"])  # the whole chain, flattened
        self.assertIn("SECRET_ISH", current["text"])
        self.assertEqual([("work", True), ("base", True)],
                         [(t["name"], t["in_chain"]) for t in m["bottle_targets"]])

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

    def test_the_agents_version_plus_this_runs_changes(self) -> None:
        from bot_bottle import run_manifest  # pylint: disable=import-outside-toplevel
        from bot_bottle.control.git_gate_render import (  # pylint: disable=import-outside-toplevel
            GitGateUpstream)
        from bot_bottle.git_gate.provision import (  # pylint: disable=import-outside-toplevel
            _write_upstreams_snapshot)
        self.grant("app", "gitea.example.com/o/app")
        _write_upstreams_snapshot(git_gate_state_dir("s1"), (GitGateUpstream(
            name="app", upstream_url="ssh://git@h/o/app.git", upstream_host="h",
            upstream_port="", identity_file="", known_host_key="ssh-ed25519 K",
            writable=False),))
        _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
        current = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
        self.assertEqual([{"kind": "egress", "what": "docs.example.com"},
                          {"kind": "repo", "what": "app (read-only)"}], current["changes"])
        self.assertEqual(["keyed.example.com"], [u["what"] for u in current["unkept"]])
        repo = current["data"]["git-gate"]["repos"]["app"]
        self.assertEqual(("ssh://git@h/o/app.git", True, "ssh-ed25519 K"),
                         (repo["url"], repo["key"]["read_only"], repo["host_key"]))
        self.assertNotIn("docs.example.com", current["base"])
        self.assertIn("docs.example.com", current["text"])
        self.assertEqual(1, len(run_manifest.versions("s1")))  # v1 made once

    def test_save_a_version_and_optionally_the_bottle(self) -> None:
        from bot_bottle import run_manifest  # pylint: disable=import-outside-toplevel
        from bot_bottle.manifest.versions import history  # pylint: disable=import-outside-toplevel
        work = Path(os.environ["BOT_BOTTLE_ROOT"]) / "bottles" / "work.md"
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY):
            _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "changed since"):
                manifest_view.save_manifest("s1", "stale")
            self.assertEqual({"version": 2}, manifest_view.save_manifest("s1", shown["digest"]))
            self.assertNotIn("docs.example.com", work.read_text())  # the file untouched
            after = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            self.assertEqual(([], after["base"]), (after["changes"], after["text"]))
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "nothing new"):
                manifest_view.save_manifest("s1", after["digest"], bottle="work")
            self.assertEqual("saved: docs.example.com", run_manifest.versions("s1")[-1]["message"])
        # A fresh run of changes, saved to the bottle file as well.
        run_manifest.record("s1", run_manifest.read("s1", 1), "back to the start")
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY):
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "no bottle nope"):
                manifest_view.save_manifest("s1", shown["digest"], bottle="nope")
            saved = manifest_view.save_manifest("s1", shown["digest"], bottle="base")
        self.assertEqual({"version": 4, "bottle": "base"}, saved)
        base = work.parent / "base.md"
        self.assertIn("docs.example.com", base.read_text())
        self.assertNotIn("docs.example.com", work.read_text())
        self.assertTrue(history(base, root=Path(os.environ["BOT_BOTTLE_ROOT"]))[0]
                        .message.startswith("keep from the console: docs.example.com"))

    def test_save_as_a_new_bottle(self) -> None:
        from bot_bottle import run_manifest  # pylint: disable=import-outside-toplevel
        from bot_bottle.contract.yaml_subset import parse_frontmatter  # pylint: disable=import-outside-toplevel
        from bot_bottle.manifest.loader import load_bottle_chain  # pylint: disable=import-outside-toplevel
        root = Path(os.environ["BOT_BOTTLE_ROOT"])
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY):
            _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            for name, why in (("work", "already a bottle"), ("../x", "letters, digits"),
                              ("./main", "letters, digits")):
                with self.assertRaisesRegex(manifest_view.ManifestViewError, why):
                    manifest_view.save_as_bottle("s1", shown["digest"], name)
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "changed since"):
                manifest_view.save_as_bottle("s1", "stale", "snap")
            saved = manifest_view.save_as_bottle("s1", shown["digest"], "snap")
        self.assertEqual({"version": 2, "new_bottle": "snap"}, saved)
        text = (root / "bottles" / "snap.md").read_text()
        self.assertNotIn("extends", parse_frontmatter(text)[0])
        self.assertIn("helper's run", text)
        snap, _ = load_bottle_chain("snap", [(root / "bottles", False)])
        self.assertEqual({"LANG": "C.UTF-8", "SECRET_ISH": "literal-value"}, dict(snap.env))
        self.assertIn("docs.example.com", [r.Host for r in snap.egress.routes])
        self.assertEqual("saved as bottle snap", run_manifest.versions("s1")[-1]["message"])

    def test_save_as_a_bottle_extending_its_bottles(self) -> None:
        from bot_bottle.contract.yaml_subset import parse_frontmatter  # pylint: disable=import-outside-toplevel
        from bot_bottle.manifest.loader import load_bottle_chain  # pylint: disable=import-outside-toplevel
        root = Path(os.environ["BOT_BOTTLE_ROOT"])
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY):
            _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            manifest_view.save_as_bottle("s1", shown["digest"], "more", extend=True)
        fm = parse_frontmatter((root / "bottles" / "more.md").read_text())[0]
        self.assertEqual(["work"], fm["extends"])
        self.assertNotIn("env", fm)  # only the run's changes
        more, _ = load_bottle_chain("more", [(root / "bottles", False)])
        self.assertEqual({"LANG": "C.UTF-8", "SECRET_ISH": "literal-value"}, dict(more.env))
        self.assertEqual(["example.com", "docs.example.com"], [r.Host for r in more.egress.routes])

    def test_extending_a_project_bottle_is_refused(self) -> None:
        write_metadata(BottleMetadata(identity="s1", agent_name="helper", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=("./main",)))
        with mock.patch.object(manifest_view, "_load",
                               return_value=(mock.Mock(bottle_names=("./main",)), mock.Mock())), \
                mock.patch.object(manifest_view, "current_manifest", return_value={"digest": "d"}), \
                mock.patch.object(manifest_view, "_live_policy", return_value=None):
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "project's bottle"):
                manifest_view.save_as_bottle("s1", "d", "more", extend=True)

    def test_keep_target_falls_back_to_the_agents_bottle(self) -> None:
        from bot_bottle.approvals import keep_target  # pylint: disable=import-outside-toplevel
        write_metadata(BottleMetadata(identity="s3", agent_name="helper", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=()))
        target = keep_target("s3")
        assert target is not None
        self.assertEqual("work", target.label())


class TestManifestSaveRequest(unittest.TestCase):
    def ask(self, running: dict[str, str], digest: str = "d", **msg: object) -> dict[str, object]:
        return asyncio.run(remote_actions.manifest_save(
            {"request_id": "q", "agent_id": "a1", "digest": digest, **msg}, running))

    def test_reply(self) -> None:
        with mock.patch.object(manifest_view, "save_manifest",
                               return_value={"version": 2, "bottle": "work"}) as save:
            reply = self.ask({"a1": "s1"}, bottle="work")
        save.assert_called_once_with("s1", "d", bottle="work")
        self.assertEqual(("manifest_saved", 2, "work"),
                         (reply["type"], reply["version"], reply["bottle"]))
        with mock.patch.object(manifest_view, "save_manifest", return_value={"version": 3}) as save:
            self.ask({"a1": "s1"}, bottle=True)  # a name, not a flag
        save.assert_called_once_with("s1", "d", bottle="")
        with mock.patch.object(manifest_view, "save_as_bottle",
                               return_value={"version": 4, "new_bottle": "snap"}) as save_as:
            reply = self.ask({"a1": "s1"}, new_bottle="snap", extend=True)
        save_as.assert_called_once_with("s1", "d", "snap", extend=True)
        self.assertEqual("snap", reply["new_bottle"])

    def test_errors(self) -> None:
        self.assertIn("isn't running", str(self.ask({})["error"]))
        with mock.patch.object(manifest_view, "save_manifest",
                               side_effect=manifest_view.ManifestViewError("changed")):
            self.assertEqual("changed", self.ask({"a1": "s1"})["error"])
        with mock.patch.object(manifest_view, "save_manifest", side_effect=OSError("ro")):
            self.assertIn("host controller's log", str(self.ask({"a1": "s1"})["error"]))
