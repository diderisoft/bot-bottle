"""The host's agents and bottles for the console's Library (PRD 0109)."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.control.hostctl import library, remote_actions


class TestHostLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        root = self.home / ".bot-bottle"
        (root / "agents").mkdir(parents=True)
        (root / "bottles").mkdir()
        (root / "agents" / "helper.md").write_text("---\nbottle: work\n---\nHelps.\n")
        (root / "bottles" / "work.md").write_text("---\nsupervise: true\n---\nWork.\n")
        (root / "bottles" / "big.md").write_text("x" * (library.MAX_BYTES + 10))

    def test_home_files_and_unshadowed_builtins(self) -> None:
        with mock.patch.object(Path, "home", return_value=self.home):
            got = library.host_library()
        self.assertEqual([{"name": "helper", "content": "---\nbottle: work\n---\nHelps.\n"}],
                         got["agents"])
        bottles = {b["name"]: b for b in got["bottles"]}
        self.assertEqual(("---\nsupervise: true\n---\nWork.\n", False),
                         (bottles["work"]["content"], bottles["work"]["builtin"]))
        self.assertTrue(bottles["big"]["content"].endswith("over 64 KiB)\n"))
        self.assertTrue(any(b["builtin"] for b in got["bottles"]))  # e.g. basic
        self.assertEqual(sorted(bottles), [b["name"] for b in got["bottles"]])

    def test_no_home_dir_is_empty(self) -> None:
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict(os.environ, {"HOME": str(empty)}), \
                mock.patch.object(Path, "home", return_value=empty):
            self.assertEqual({"agents": [], "bottles": []}, library.host_library())

    def test_the_query(self) -> None:
        with mock.patch.object(library, "host_library",
                               return_value={"agents": [], "bottles": []}):
            reply = asyncio.run(remote_actions.QUERIES["library"]({"request_id": "r1"}, {}, None))
        self.assertEqual({"type": "library", "request_id": "r1", "agents": [], "bottles": []},
                         reply)
        with mock.patch.object(library, "host_library", side_effect=RuntimeError("boom")):
            reply = asyncio.run(remote_actions.library({"request_id": "r2"}, {}))
        self.assertIn("error", reply)


class TestResolvedManifest(unittest.TestCase):
    """The launch page's preview: an agent in bottles, merged as a launch would."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        root = self.home / ".bot-bottle"
        (root / "agents").mkdir(parents=True)
        (root / "bottles").mkdir()
        (root / "agents" / "helper.md").write_text("---\nbottle: work\n---\nHelps.\n")
        (root / "bottles" / "work.md").write_text("---\nenv:\n  A: \"1\"\n  B: \"1\"\n---\n")
        (root / "bottles" / "more.md").write_text("---\nenv:\n  B: \"2\"\n---\n")

    def _resolve(self, msg: dict) -> dict:
        with mock.patch.object(Path, "home", return_value=self.home):
            return asyncio.run(remote_actions.QUERIES["resolve"]({"request_id": "r", **msg}, {}))

    def test_the_bottles_merge_in_order(self) -> None:
        reply = self._resolve({"agent": "helper", "bottles": ["work", "more"]})
        self.assertEqual(("resolved", "r", ["work", "more"]),
                         (reply["type"], reply["request_id"], reply["bottles"]))
        self.assertEqual({"A": "1", "B": "2"}, reply["data"]["env"])
        self.assertIn("env:", reply["text"])
        reply = self._resolve({"agent": "helper", "bottles": ["more", "work"]})
        self.assertEqual({"A": "1", "B": "1"}, reply["data"]["env"])

    def test_no_bottles_is_the_agents_own(self) -> None:
        reply = self._resolve({"agent": "helper", "bottles": []})
        self.assertEqual((["work"], {"A": "1", "B": "1"}), (reply["bottles"], reply["data"]["env"]))

    def test_what_doesnt_resolve_says_why(self) -> None:
        self.assertIn("not defined", self._resolve({"agent": "nobody"})["error"])
        self.assertIn("error", self._resolve({"agent": "helper", "bottles": ["missing"]}))
        self.assertIn("error", self._resolve({"agent": "../x"}))
        self.assertIn("error", self._resolve({"agent": "helper", "bottles": "work"}))


if __name__ == "__main__":
    unittest.main()
