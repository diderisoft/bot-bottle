"""A running agent's manifest as elements with where they come from, and
copying or moving them into another bottle."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.bottle_state import BottleMetadata, egress_state_dir, git_gate_state_dir, \
    write_metadata
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.control.hostctl import manifest_elements, manifest_view

AGENT = "---\nbottle: work\n---\n\nHi.\n"
WORK = """---
extends: base
env:
  LANG: en_GB
egress:
  routes:
    - host: example.com
---

Work.
"""
BASE = """---
env:
  LANG: C.UTF-8
  TZ: UTC
egress:
  routes:
    - host: docs.example.com
---

Base.
"""
GITEA = """---
credentials:
  gitea-api:
    source: secret
    secret: GITEA_TOKEN
git-gate:
  forges:
    gitea.example.com:
      kind: gitea
      url: https://gitea.example.com
      ssh: ssh://git@gitea.example.com:2222
      token: $gitea-api
---
"""
LAUNCH = 'routes:\n  - host: "api.example.com"\n    inspect:\n      auth_scheme: "Bearer"\n' \
         '      token_env: "EGRESS_TOKEN_0"\n  - host: "example.com"\n  - host: "docs.example.com"\n'
LIVE = LAUNCH + '  - host: "new.example.com"\n'


class _Chain(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.root = home / ".bot-bottle"
        (self.root / "agents").mkdir(parents=True)
        self.bottles = self.root / "bottles"
        self.bottles.mkdir()
        (self.root / "agents" / "helper.md").write_text(AGENT)
        for name, text in (("work", WORK), ("base", BASE), ("gitea", GITEA),
                           ("other", "---\nenv:\n  A: b\n---\n")):
            (self.bottles / f"{name}.md").write_text(text)
        env = mock.patch.dict(os.environ, {"HOME": str(home), "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        write_metadata(BottleMetadata(identity="s1", agent_name="helper", cwd="",
                                      copy_cwd=False, started_at="", bottle_names=("work",)))
        egress_state_dir("s1").mkdir(parents=True)
        (egress_state_dir("s1") / "routes.yaml").write_text(LAUNCH)
        for target, value in (("_live_policy", LIVE),):
            patcher = mock.patch.object(manifest_view, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        project = mock.patch("bot_bottle.approvals.session_project", return_value=None)
        project.start()
        self.addCleanup(project.stop)

    def fm(self, name: str) -> dict[str, Any]:
        return dict(parse_frontmatter((self.bottles / f"{name}.md").read_text())[0])


class TestElements(_Chain):
    def test_each_with_where_it_comes_from(self) -> None:
        rows = {r["key"]: r for r in manifest_elements.elements("s1", LIVE)}
        self.assertEqual(("work", ["base"], "en_GB"),
                         (rows["env:LANG"]["source"], rows["env:LANG"]["also"],
                          rows["env:LANG"]["detail"]))
        self.assertEqual("base", rows["env:TZ"]["source"])
        self.assertEqual("work", rows["egress:example.com"]["source"])
        self.assertEqual("base", rows["egress:docs.example.com"]["source"])
        self.assertEqual(("this run", False), (rows["egress:new.example.com"]["source"],
                                               rows["egress:new.example.com"]["fixed"]))
        self.assertEqual(("agent provider", True), (rows["egress:api.example.com"]["source"],
                                                    rows["egress:api.example.com"]["fixed"]))
        self.assertEqual(["egress", "egress", "egress", "egress", "env", "env"],
                         [r["kind"] for r in manifest_elements.elements("s1", LIVE)])


class TestTransfer(_Chain):
    def test_copy_leaves_the_source(self) -> None:
        reply = manifest_elements.transfer("s1", ["env:TZ", "egress:docs.example.com"], "other",
                                           move=False)
        self.assertEqual({"into": "other", "moved_from": [], "count": 2}, reply)
        self.assertEqual({"A": "b", "TZ": "UTC"}, self.fm("other")["env"])
        self.assertEqual(["docs.example.com"], [r["host"] for r in self.fm("other")["egress"]["routes"]])
        self.assertIn("TZ", self.fm("base")["env"])

    def test_move_takes_it_out_of_its_file(self) -> None:
        reply = manifest_elements.transfer("s1", ["env:TZ", "egress:example.com"], "other",
                                           move=True)
        self.assertEqual(["base", "work"], reply["moved_from"])
        self.assertNotIn("TZ", self.fm("base")["env"])
        self.assertEqual([], self.fm("work")["egress"]["routes"])
        self.assertEqual("UTC", self.fm("other")["env"]["TZ"])

    def test_this_runs_route_and_repo(self) -> None:
        from bot_bottle.control.git_gate_render import (  # pylint: disable=import-outside-toplevel
            GitGateUpstream)
        from bot_bottle.git_gate.provision import (  # pylint: disable=import-outside-toplevel
            DeployKeyRef, _write_upstreams_snapshot, record_live_grant)
        stage = git_gate_state_dir("s1")
        stage.mkdir(parents=True)
        record_live_grant(stage, DeployKeyRef("app", "GITEA_TOKEN", "", "h", "acme/app.git",
                                              grant="gitea.example.com/acme/app"))
        record_live_grant(stage, DeployKeyRef("lib", "T", "", "h", "acme/lib.git"))
        _write_upstreams_snapshot(stage, tuple(GitGateUpstream(
            name=n, upstream_url=f"ssh://git@h/acme/{n}.git", upstream_host="h",
            upstream_port="", identity_file="", known_host_key="", writable=True)
            for n in ("app", "lib")))
        rows = {r["key"]: r for r in manifest_elements.elements("s1", None)}
        self.assertEqual("write · gitea.example.com/acme/app", rows["repo:app"]["detail"])
        self.assertEqual("write · ssh://git@h/acme/lib.git", rows["repo:lib"]["detail"])
        # The grant's forge, from the host bottle declaring it, listed but kept there.
        self.assertEqual(("gitea", True), (rows["forge:gitea.example.com"]["source"],
                                           rows["forge:gitea.example.com"]["fixed"]))
        # A repo granted with no grant on record can't go into a bottle (ADR 0008).
        with self.assertRaisesRegex(manifest_view.ManifestViewError, "lib was granted"):
            manifest_elements.transfer("s1", ["repo:lib"], "other", move=True)
        manifest_elements.transfer("s1", ["egress:new.example.com", "repo:app"],
                                   "other", move=True)
        other = self.fm("other")
        self.assertEqual(["new.example.com"], [r["host"] for r in other["egress"]["routes"]])
        self.assertEqual({"app": "gitea.example.com/acme/app"}, other["git-gate"]["repos"])
        self.assertEqual(["gitea"], other["extends"])  # where the grant's forge is

    def test_refusals(self) -> None:
        for keys, target, why in (([], "other", "pick the elements"),
                                  (["env:NOPE"], "other", "no env:NOPE"),
                                  (["env:LANG"], "work", "already in work"),
                                  (["egress:api.example.com"], "other", "can't be written"),
                                  (["env:TZ"], "nowhere", "no bottle nowhere")):
            with self.subTest(keys=keys), \
                    self.assertRaisesRegex(manifest_view.ManifestViewError, why):
                manifest_elements.transfer("s1", keys, target, move=False)


class TestDelete(_Chain):
    def test_out_of_their_files(self) -> None:
        reply = manifest_elements.delete("s1", ["env:TZ", "egress:example.com"])
        self.assertEqual({"deleted_from": ["base", "work"], "removed": [], "count": 2}, reply)
        self.assertNotIn("TZ", self.fm("base")["env"])
        self.assertEqual([], self.fm("work")["egress"]["routes"])

    def test_this_runs_go_from_the_run(self) -> None:
        with mock.patch.object(manifest_view, "remove_from_run",
                               return_value={"removed": ["new.example.com"]}) as remove:
            reply = manifest_elements.delete("s1", ["egress:new.example.com"], backend="b")
        self.assertEqual(["new.example.com"], reply["removed"])
        self.assertEqual(["egress:new.example.com"], remove.call_args.args[2])
        self.assertEqual("b", remove.call_args.kwargs["backend"])

    def test_refusals(self) -> None:
        for keys, why in (([], "pick the elements"), (["env:NOPE"], "no env:NOPE"),
                          (["egress:api.example.com"], "can't be deleted")):
            with self.subTest(keys=keys), \
                    self.assertRaisesRegex(manifest_view.ManifestViewError, why):
                manifest_elements.delete("s1", keys)


class TestTransferRequest(unittest.TestCase):
    def test_reply(self) -> None:
        import asyncio  # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl import remote_actions  # pylint: disable=import-outside-toplevel
        ask = {"request_id": "q", "agent_id": "a1", "keys": ["env:A", 3], "target": "t",
               "move": True}
        with mock.patch.object(manifest_elements, "transfer",
                               return_value={"into": "t", "count": 1}) as transfer:
            reply = asyncio.run(remote_actions.manifest_transfer(ask, {"a1": "s1"}))
        transfer.assert_called_once_with("s1", ["env:A"], "t", move=True)
        bottles = mock.Mock()
        bottles.backend_for_session.return_value = "the backend"
        with mock.patch.object(manifest_elements, "delete", return_value={"count": 1}) as delete:
            asyncio.run(remote_actions.manifest_transfer({**ask, "delete": True}, {"a1": "s1"},
                                                         bottles))
        delete.assert_called_once_with("s1", ["env:A"], backend="the backend")
        self.assertEqual(("manifest_transferred", "t"), (reply["type"], reply["into"]))
        self.assertIn("isn't running", asyncio.run(remote_actions.manifest_transfer(ask, {}))["error"])
        with mock.patch.object(manifest_elements, "transfer",
                               side_effect=manifest_view.ManifestViewError("no")):
            self.assertEqual("no", asyncio.run(remote_actions.manifest_transfer(ask, {"a1": "s1"}))["error"])
        with mock.patch.object(manifest_elements, "transfer", side_effect=OSError("ro")):
            self.assertIn("host controller's log",
                          asyncio.run(remote_actions.manifest_transfer(ask, {"a1": "s1"}))["error"])


if __name__ == "__main__":
    unittest.main()
