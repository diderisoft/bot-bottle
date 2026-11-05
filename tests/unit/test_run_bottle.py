"""A run's unsaved bottle (ADR 0017): staged for its launch, kept in its state
dir for a resume and the console, recorded in its audit, never in the
library, and only from the `cli` role."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import run_bottle
from bot_bottle.contract.auth import ROLE_CLI
from bot_bottle.contract.hostctl import (
    ROLE_ORCHESTRATOR,
    RUN_BOTTLE,
    RUN_BOTTLE_MAX_BYTES,
    LaunchRequest,
)
from bot_bottle.manifest import ManifestIndex

TEXT = "---\negress:\n  routes:\n    - host: api.open-meteo.com\n---\n\nFor this run.\n"


class TestRunBottle(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.home / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)
        (self.root / "agents").mkdir()
        (self.root / "agents" / "claude.md").write_text("---\nagent_provider: claude\n---\n\nHi.\n")
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                             "BOT_BOTTLE_ROOT": str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        self.index = ManifestIndex.from_md_dirs(self.root, None)

    def test_staged_for_the_launch_and_loadable_but_not_in_the_library(self) -> None:
        index = run_bottle.index_for(self.index, text=TEXT)
        staged = index.extra_bottle_dirs[0]
        self.assertTrue(run_bottle.is_run_path(staged / f"{RUN_BOTTLE}.md"))
        manifest = index.load_for_agent("claude", ("basic", RUN_BOTTLE))
        self.assertIn("api.open-meteo.com", [r.Host for r in manifest.bottle.egress.routes])
        self.assertNotIn(RUN_BOTTLE, ManifestIndex.from_md_dirs(self.root, None).all_bottle_names)
        self.assertEqual([], list((self.root / "bottles").iterdir()))

    def test_kept_in_the_state_dir_for_a_resume(self) -> None:
        run_bottle.keep("bb-1", run_bottle.index_for(self.index, text=TEXT))
        kept = run_bottle.state_dir("bb-1") / f"{RUN_BOTTLE}.md"
        self.assertEqual(TEXT, kept.read_text())
        self.assertTrue(run_bottle.is_run_path(kept))
        resumed = run_bottle.index_for(self.index, identity="bb-1")
        self.assertEqual((run_bottle.state_dir("bb-1"),), resumed.extra_bottle_dirs)
        self.assertIs(self.index, run_bottle.index_for(self.index, identity="bb-other"))
        self.assertIs(self.index, run_bottle.index_for(self.index))
        run_bottle.keep("bb-2", self.index)  # nothing staged: nothing kept
        self.assertFalse(run_bottle.state_dir("bb-2").exists())

    def test_a_library_bottle_is_not_a_run_bottle(self) -> None:
        self.assertFalse(run_bottle.is_run_path(self.root / "bottles" / f"{RUN_BOTTLE}.md"))
        self.assertFalse(run_bottle.is_run_path(run_bottle.state_dir("bb-1") / "basic.md"))

    def test_the_audit_gets_its_full_text(self) -> None:
        self.assertEqual({"host": 3, "run_bottle": TEXT},
                         run_bottle.with_content({"host": 3}, TEXT))
        self.assertEqual({"host": 3}, run_bottle.with_content({"host": 3}, ""))


class TestLaunchRequest(unittest.TestCase):
    def test_run_bottle_is_cli_only_named_in_bottles_and_capped(self) -> None:
        body = {"agent": "claude", "bottles": ["basic", RUN_BOTTLE], "run_bottle": TEXT}
        self.assertEqual(TEXT, LaunchRequest.from_json(body, ROLE_CLI).run_bottle)
        with self.assertRaises(PermissionError):
            LaunchRequest.from_json(body, ROLE_ORCHESTRATOR)
        LaunchRequest(agent="claude", bottles=("basic", RUN_BOTTLE), run_bottle=TEXT).check()
        for bad in (LaunchRequest(agent="claude", bottles=("basic",), run_bottle=TEXT),
                    LaunchRequest(agent="claude", bottles=(RUN_BOTTLE,),
                                  run_bottle="x" * (RUN_BOTTLE_MAX_BYTES + 1))):
            with self.subTest(bad=bad.bottles), self.assertRaises(ValueError):
                bad.check()


if __name__ == "__main__":
    unittest.main()
