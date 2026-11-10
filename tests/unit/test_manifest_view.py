"""A running agent's manifest for the console's Manifest tab."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.bottle_state import BottleMetadata, git_gate_state_dir, write_metadata
from bot_bottle.control.hostctl import manifest_view, remote_actions
from bot_bottle import forge_connections
from tests.unit._forges import record

AGENT = """---
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
      allow:
        - methods: [GET]
          paths: [/v1/]
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


# A host bottle describing the forge this run's grants are on; the host's
# connection to it (`forge_connections`) is what grants use (ADR 0019).
GITEA = """---
git_gate:
  forges:
    gitea.example.com:
      kind: gitea
---
"""
CONN = forge_connections.ForgeConnection(
    host="gitea.example.com", kind="gitea", url="https://gitea.example.com",
    ssh="ssh://git@h",
    users=(forge_connections.ForgeUser("bot", "GITEA_TOKEN", default=True),))


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
        (root / "bottles" / "gitea.md").write_text(GITEA)
        record(root, CONN)
        env = mock.patch.dict(os.environ, {"HOME": str(home), "BOT_BOTTLE_ROOT": str(root)})
        env.start()
        self.addCleanup(env.stop)
        self.root = root
        write_metadata(BottleMetadata(identity="s1", agent_name="helper", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=("work",)))

    def versions(self) -> list[dict[str, Any]]:
        with mock.patch.object(manifest_view, "_live_policy", return_value=None):
            return manifest_view.agent_manifest("s1")["bottle_versions"]

    def test_whether_each_bottle_is_still_what_it_launched_with(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle import run_manifest
        from bot_bottle.manifest.versions import launch_versions
        # Launched before launches recorded their bottles: not known.
        self.assertEqual([("work", False, False)],
                         [(v["name"], v["known"], v["latest"]) for v in self.versions()])
        host = str(launch_versions(str(self.root.parent))["host"])
        bottle = manifest_view._load("s1")[1].bottle  # pylint: disable=protected-access
        run_manifest.record_launch("s1", bottle, names=("work",), host_version=host)
        work = self.versions()[0]
        self.assertEqual((True, True, False), (work["known"], work["latest"], work["project"]))
        self.assertEqual(host, work["version"])
        self.assertGreater(work["version_at"], 0)
        # The same bottle written in another order is still the one it launched with.
        (self.root / "bottles" / "base.md").write_text("---\nenv:\n  LANG: C.UTF-8\n---\n\nBase.\n")
        self.assertTrue(self.versions()[0]["latest"])
        # A change to a bottle it extends makes it not the latest.
        (self.root / "bottles" / "base.md").write_text(BASE.replace("C.UTF-8", "C"))
        self.assertFalse(self.versions()[0]["latest"])

    def test_what_it_resolves_to(self) -> None:
        with mock.patch.object(manifest_view, "_live_policy", return_value=None):
            m = manifest_view.agent_manifest("s1")
        self.assertEqual(("helper", ["work"], "pi"), (m["agent"], m["bottles"], m["harness"]))
        current = m["current"]
        self.assertEqual((1, "the bottle files when first viewed", []),
                         (current["version"], current["message"], current["changes"]))
        self.assertIn("LANG: C.UTF-8", current["text"])  # the whole chain, flattened
        self.assertIn("SECRET_ISH", current["text"])
        self.assertEqual([("work", True), ("base", True), ("gitea", False)],
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
                mock.patch("bot_bottle.control.client.env_orchestrator_url",
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
        self.assertEqual([{"kind": "egress", "what": "docs.example.com",
                           "key": "egress:docs.example.com"},
                          {"kind": "repo", "what": "gitea.example.com/o/app:write",
                           "key": "repo:app"}],
                         r["changes"])
        self.assertEqual(["keyed.example.com"], [u["what"] for u in r["unkept"]])
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
        self.assertEqual([{"kind": "egress", "what": "docs.example.com",
                           "key": "egress:docs.example.com"},
                          {"kind": "repo", "what": "app (read-only)", "key": "repo:app"}],
                         current["changes"])
        self.assertEqual(["keyed.example.com"], [u["what"] for u in current["unkept"]])
        # Granted on the gitea bottle's forge: a grant, the forge with it.
        git = current["data"]["git_gate"]
        self.assertEqual("gitea.example.com/o/app", git["repos"]["app"])
        self.assertIn("gitea.example.com", git["forges"])
        self.assertNotIn("docs.example.com", current["base"])
        self.assertIn("docs.example.com", current["text"])
        self.assertEqual(1, len(run_manifest.versions("s1")))  # v1 made once

    def test_a_grant_shows_as_its_grant_with_its_forge(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.git_gate_render import GitGateUpstream
        from bot_bottle.git_gate.provision import _write_upstreams_snapshot
        from bot_bottle.manifest.forges import ManifestForge
        self.grant("app", "gitea.example.com/o/app:write")
        _write_upstreams_snapshot(git_gate_state_dir("s1"), (GitGateUpstream(
            name="app", upstream_url="ssh://git@h/o/app.git", upstream_host="h",
            upstream_port="", identity_file="", known_host_key="", writable=True),))
        forge = ManifestForge("gitea.example.com", "gitea", "https://gitea.example.com",
                              "ssh://git@h", "GITEA_TOKEN")
        _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
        with mock.patch("bot_bottle.control.hostctl.repo_grants.find_forge",
                        return_value=forge):
            current = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
        git = current["data"]["git_gate"]
        self.assertEqual("gitea.example.com/o/app:write", git["repos"]["app"])
        # Described, never its token (ADR 0019).
        self.assertEqual({"kind": "gitea"}, git["forges"]["gitea.example.com"])
        self.assertNotIn("credentials", current["data"])
        self.assertIn("gitea.example.com/o/app:write", current["text"])

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

    def test_save_only_the_picked_changes(self) -> None:
        from bot_bottle import run_manifest  # pylint: disable=import-outside-toplevel
        self.grant("app", "gitea.example.com/o/app")
        from bot_bottle.control.git_gate_render import (  # pylint: disable=import-outside-toplevel
            GitGateUpstream)
        from bot_bottle.git_gate.provision import (  # pylint: disable=import-outside-toplevel
            _write_upstreams_snapshot)
        _write_upstreams_snapshot(git_gate_state_dir("s1"), (GitGateUpstream(
            name="app", upstream_url="ssh://git@h/o/app.git", upstream_host="h",
            upstream_port="", identity_file="", known_host_key="", writable=True),))
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY):
            _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            self.assertEqual(["egress:docs.example.com", "repo:app"],
                             [c["key"] for c in shown["changes"]])
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "none of the picked"):
                manifest_view.save_manifest("s1", shown["digest"], keys=["egress:keyed.example.com"])
            manifest_view.save_manifest("s1", shown["digest"], keys=["repo:app"])
            after = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
        self.assertEqual(["egress:docs.example.com"], [c["key"] for c in after["changes"]])
        self.assertIn("app", run_manifest.latest("s1")[1]["git_gate"]["repos"])  # type: ignore[index]

    def test_a_grant_without_its_grant_string_cant_be_kept(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.approvals import KeepTarget
        from bot_bottle.control.git_gate_render import GitGateUpstream
        from bot_bottle.git_gate.provision import _write_upstreams_snapshot
        self.grant("app", "")
        _write_upstreams_snapshot(git_gate_state_dir("s1"), (GitGateUpstream(
            name="app", upstream_url="ssh://git@h/o/app.git", upstream_host="h",
            upstream_port="", identity_file="", known_host_key="ssh-ed25519 K",
            writable=False),))
        work = Path(os.environ["BOT_BOTTLE_ROOT"]) / "bottles" / "work.md"
        into = manifest_view.bottle_with_changes("s1", LAUNCH_POLICY, KeepTarget(work, None))
        self.assertEqual([], into["changes"])
        self.assertEqual([("repo:app", manifest_view.GRANTLESS)],
                         [(u["key"], u["why"]) for u in into["unkept"]])
        self.assertNotIn("ssh://git@h/o/app.git", into["updated"])  # no long form written
        _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
        shown = manifest_view.current_manifest("s1", manifest.bottle, LAUNCH_POLICY)
        self.assertEqual(["repo:app"], [u["key"] for u in shown["unkept"]])
        self.assertNotIn("ssh://git@h/o/app.git", shown["text"])

    def test_a_bottle_that_cant_take_the_picks_saves_nothing(self) -> None:
        from bot_bottle import run_manifest  # pylint: disable=import-outside-toplevel
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY):
            _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            manifest_view.save_manifest("s1", shown["digest"], bottle="work")
            again = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            run_manifest.record("s1", run_manifest.read("s1", 1), "back")  # unsaved again
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            before = len(run_manifest.versions("s1"))
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "nothing was saved"):
                manifest_view.save_manifest("s1", shown["digest"], bottle="work")
        self.assertEqual([], again["changes"])
        self.assertEqual(before, len(run_manifest.versions("s1")))

    def test_remove_routes_back_to_launch(self) -> None:
        from bot_bottle.bottle_state import egress_state_dir  # pylint: disable=import-outside-toplevel
        from bot_bottle.gateway.egress.schema import load_config  # pylint: disable=import-outside-toplevel
        (egress_state_dir("s1") / "routes.yaml").write_text(LAUNCH_POLICY + '  - host: "docs.example.com"\n'
                                                            '    inspect:\n      matches:\n'
                                                            '        - methods: ["GET"]\n')
        client = mock.Mock()
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY), \
                mock.patch("bot_bottle.backend.adoption.read",
                           return_value=mock.Mock(registry_id="reg-1", deploy_keys=())), \
                mock.patch("bot_bottle.control.client.env_orchestrator_url",
                           return_value="http://o"), \
                mock.patch("bot_bottle.control.client.OrchestratorClient", return_value=client):
            _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
            shown = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "pick the changes"):
                manifest_view.remove_changes("s1", shown["digest"], [])
            reply = manifest_view.remove_changes(
                "s1", shown["digest"], ["egress:docs.example.com", "egress:keyed.example.com"])
        self.assertEqual({"removed": ["docs.example.com", "keyed.example.com"]}, reply)
        bottle_id, text = client.set_policy.call_args.args
        routes = {r.host: r for r in load_config(text).routes}
        self.assertEqual("reg-1", bottle_id)
        self.assertEqual({"api.example.com", "docs.example.com"}, set(routes))  # keyed: gone
        self.assertEqual(("GET",), routes["docs.example.com"].matches[0].methods)  # as launched

    def test_remove_repos_through_the_backend(self) -> None:
        backend = mock.Mock()
        self.grant("app", "gitea.example.com/o/app")
        with mock.patch.object(manifest_view, "_live_policy", return_value=LIVE_POLICY), \
                mock.patch("bot_bottle.backend.adoption.read",
                           return_value=mock.Mock(registry_id="reg-1", deploy_keys=())), \
                mock.patch("bot_bottle.git_gate.live_grant.revoke_live") as revoke:
            _, manifest = manifest_view._load("s1")  # pylint: disable=protected-access
            digest = manifest_view.current_manifest("s1", manifest.bottle, LIVE_POLICY)["digest"]
            with self.assertRaisesRegex(manifest_view.ManifestViewError, "can't remove repos"):
                manifest_view.remove_changes("s1", digest, ["repo:app"])
            self.assertEqual({"removed": ["app"]}, manifest_view.remove_changes(
                "s1", digest, ["repo:app"], backend=backend))
        self.assertEqual("app", revoke.call_args.args[0])
        self.assertEqual("reg-1", revoke.call_args.kwargs["registry_id"])

    def test_with_no_bottles_picked_keeping_needs_a_basic_of_your_own(self) -> None:
        # None picked means `basic` (ADR 0011): the built-in can't be written,
        # a `basic` of your own can.
        from bot_bottle.approvals import keep_target  # pylint: disable=import-outside-toplevel
        write_metadata(BottleMetadata(identity="s3", agent_name="helper", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=()))
        self.assertIsNone(keep_target("s3"))
        (self.root / "bottles" / "basic.md").write_text("---\n---\n")
        target = keep_target("s3")
        assert target is not None
        self.assertEqual("basic", target.label())


class TestManifestSaveRequest(unittest.TestCase):
    def ask(self, running: dict[str, str], digest: str = "d", **msg: object) -> dict[str, object]:
        return asyncio.run(remote_actions.manifest_save(
            {"request_id": "q", "agent_id": "a1", "digest": digest, **msg}, running))

    def test_reply(self) -> None:
        with mock.patch.object(manifest_view, "save_manifest",
                               return_value={"version": 2, "bottle": "work"}) as save:
            reply = self.ask({"a1": "s1"}, bottle="work")
        save.assert_called_once_with("s1", "d", bottle="work", keys=None)
        self.assertEqual(("manifest_saved", 2, "work"),
                         (reply["type"], reply["version"], reply["bottle"]))
        with mock.patch.object(manifest_view, "save_manifest", return_value={"version": 3}) as save:
            self.ask({"a1": "s1"}, bottle=True)  # a name, not a flag
        save.assert_called_once_with("s1", "d", bottle="", keys=None)
        with mock.patch.object(manifest_view, "save_as_bottle",
                               return_value={"version": 4, "new_bottle": "snap"}) as save_as:
            reply = self.ask({"a1": "s1"}, new_bottle="snap", extend=True, keys=["repo:a", 5])
        save_as.assert_called_once_with("s1", "d", "snap", extend=True, keys=["repo:a"])
        bottles = mock.Mock()
        bottles.backend_for_session.return_value = "the backend"
        with mock.patch.object(manifest_view, "remove_changes",
                               return_value={"removed": ["a"]}) as remove:
            removed = asyncio.run(remote_actions.manifest_save(
                {"agent_id": "a1", "digest": "d", "remove": True, "keys": ["repo:a"]},
                {"a1": "s1"}, bottles))
        remove.assert_called_once_with("s1", "d", ["repo:a"], backend="the backend")
        self.assertEqual(["a"], removed["removed"])
        self.assertEqual("snap", reply["new_bottle"])

    def test_errors(self) -> None:
        self.assertIn("isn't running", str(self.ask({})["error"]))
        with mock.patch.object(manifest_view, "save_manifest",
                               side_effect=manifest_view.ManifestViewError("changed")):
            self.assertEqual("changed", self.ask({"a1": "s1"})["error"])
        with mock.patch.object(manifest_view, "save_manifest", side_effect=OSError("ro")):
            self.assertIn("host controller's log", str(self.ask({"a1": "s1"})["error"]))
