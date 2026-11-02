"""A running agent's bottle chain as data, and writing one of its host
bottles from the console's Bottle tab."""

from __future__ import annotations

import asyncio
import hashlib
from unittest import mock

from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.control.hostctl import bottle_chain, remote_actions
from bot_bottle.control.hostctl.manifest_view import ManifestViewError
from tests.unit.test_manifest_elements import LIVE, _Chain


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TestChain(_Chain):
    def test_files_in_merge_order_with_their_data(self) -> None:
        got = bottle_chain.chain("s1", LIVE)
        self.assertEqual([("base", "host", True), ("work", "host", True)],
                         [(b["name"], b["kind"], b["writable"]) for b in got["bottles"]])
        work = got["bottles"][1]
        self.assertEqual({"extends": "base", "env": {"LANG": "en_GB"},
                          "egress": {"routes": [{"host": "example.com"}]}}, work["frontmatter"])
        self.assertEqual("Work.", work["body"].strip())
        self.assertEqual(_digest((self.bottles / "work.md").read_text()), work["digest"])
        self.assertEqual(["base", "work"], got["targets"])
        self.assertEqual(["egress:new.example.com"], [r["key"] for r in got["run"]])

    def test_a_builtin_parent_is_listed_read_only(self) -> None:
        (self.bottles / "work.md").write_text("---\nextends: basic\n---\n")
        got = bottle_chain.chain("s1", LIVE)
        self.assertEqual([("basic", "builtin", False), ("work", "host", True)],
                         [(b["name"], b["kind"], b["writable"]) for b in got["bottles"]])
        self.assertEqual(["work"], got["targets"])


class TestWrite(_Chain):
    def digest(self, name: str) -> str:
        return _digest((self.bottles / f"{name}.md").read_text())

    def test_from_data_versions_it(self) -> None:
        with mock.patch("bot_bottle.manifest.versions.record", return_value="v2") as rec:
            reply = bottle_chain.write("s1", "base", self.digest("base"),
                                       frontmatter={"env": {"TZ": "Europe/London"}}, body="Base.")
        self.assertTrue(reply["changed"])
        self.assertEqual("v2", reply["version"])
        self.assertEqual({"env": {"TZ": "Europe/London"}}, self.fm("base"))
        self.assertEqual(self.digest("base"), reply["digest"])
        rec.assert_called_once_with("console: edit base")

    def test_from_raw_text(self) -> None:
        with mock.patch("bot_bottle.manifest.versions.record", return_value="v2"):
            bottle_chain.write("s1", "work", self.digest("work"),
                               content="---\nextends: base\nenv:\n  A: x\n---\n\nWork.")
        self.assertEqual({"extends": "base", "env": {"A": "x"}}, self.fm("work"))

    def test_a_stale_digest_is_refused(self) -> None:
        with self.assertRaises(bottle_chain.StaleBottleError):
            bottle_chain.write("s1", "base", "nope", frontmatter={}, body="")

    def test_only_this_agents_host_bottles(self) -> None:
        for name in ("other", "../agents/helper", "basic"):
            with self.subTest(name=name), self.assertRaises(ManifestViewError):
                bottle_chain.write("s1", name, "x", frontmatter={}, body="")
        (self.bottles / "work.md").write_text("---\nextends: basic\n---\n")
        with self.assertRaises(ManifestViewError) as cm:
            bottle_chain.write("s1", "basic", "x", frontmatter={}, body="")
        self.assertIn("builtin", str(cm.exception))

    def test_a_broken_manifest_is_put_back(self) -> None:
        before = (self.bottles / "work.md").read_text()
        with self.assertRaises(ManifestViewError):
            bottle_chain.write("s1", "work", self.digest("work"),
                               frontmatter={"extends": "nowhere"}, body="")
        self.assertEqual(before, (self.bottles / "work.md").read_text())

    def test_text_that_isnt_a_bottle_is_refused(self) -> None:
        before = (self.bottles / "work.md").read_text()
        with self.assertRaises(ManifestViewError):
            bottle_chain.write("s1", "work", self.digest("work"), content="---\n: :\n---\n")
        self.assertEqual(before, (self.bottles / "work.md").read_text())

    def test_a_secret_value_is_refused(self) -> None:
        (self.root / "secrets").write_text("API_TOKEN=supersecretvalue123\n")
        (self.root / "secrets").chmod(0o600)
        before = (self.bottles / "work.md").read_text()
        with self.assertRaises(ManifestViewError) as cm:
            bottle_chain.write("s1", "work", self.digest("work"),
                               frontmatter={"env": {"T": "supersecretvalue123"}}, body="")
        self.assertIn("API_TOKEN", str(cm.exception))
        self.assertEqual(before, (self.bottles / "work.md").read_text())


class TestRequests(_Chain):
    def test_chain_reply(self) -> None:
        reply = asyncio.run(remote_actions.bottle_chain({"agent_id": "a1", "request_id": "r"},
                                                        {"a1": "s1"}))
        self.assertEqual(("bottle_chain", "r"), (reply["type"], reply["request_id"]))
        self.assertEqual(["base", "work"], [b["name"] for b in reply["bottles"]])
        reply = asyncio.run(remote_actions.bottle_chain({"agent_id": "zz"}, {"a1": "s1"}))
        self.assertIn("isn't running", reply["error"])

    def test_write_reply_says_stale(self) -> None:
        reply = asyncio.run(remote_actions.bottle_write(
            {"agent_id": "a1", "request_id": "r", "bottle": "base", "digest": "old",
             "frontmatter": {}, "body": ""}, {"a1": "s1"}))
        self.assertEqual(("bottle_written", True), (reply["type"], reply["stale"]))
        digest = _digest((self.bottles / "base.md").read_text())
        with mock.patch("bot_bottle.manifest.versions.record", return_value="v2"):
            reply = asyncio.run(remote_actions.bottle_write(
                {"agent_id": "a1", "bottle": "base", "digest": digest,
                 "frontmatter": {"env": {"TZ": "UTC"}}, "body": "Base."}, {"a1": "s1"}))
        self.assertTrue(reply["changed"])
        self.assertEqual({"env": {"TZ": "UTC"}},
                         dict(parse_frontmatter((self.bottles / "base.md").read_text())[0]))
