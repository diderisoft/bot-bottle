"""Tests for architect output parsing and repair (PRD 0117, chunk 2).

The fake harness is injected by replacing `_try_claude` / `_try_codex` in the
architect module, so no real CLI is needed.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.suggest.architect import (
    ARCHITECT_MANIFEST,
    _extract_json,
    _parse_and_repair,
    architect_suggest,
)
from bot_bottle.suggest.proposal import OPTION_REUSE, OPTION_STACK_ADDITIONS


class TestExtractJson(unittest.TestCase):

    def test_plain_object(self) -> None:
        result = _extract_json('{"a": 1}')
        self.assertEqual(result, '{"a": 1}')

    def test_object_after_prose(self) -> None:
        result = _extract_json('Here is the answer:\n{"option": "reuse"}')
        self.assertEqual(result, '{"option": "reuse"}')

    def test_no_object(self) -> None:
        self.assertIsNone(_extract_json("no JSON here"))

    def test_nested_object(self) -> None:
        s = '{"a": {"b": 1}}'
        self.assertEqual(_extract_json(s), s)


class TestParseAndRepair(unittest.TestCase):

    def _valid_dict(self) -> dict:
        return {
            "option": OPTION_REUSE,
            "bottles": ["basic"],
            "new_bottle": None,
            "additions": {},
            "similar": [],
            "notes": [],
        }

    def test_valid_json_returns_proposal(self) -> None:
        raw = json.dumps(self._valid_dict())
        p = _parse_and_repair(raw)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.option, OPTION_REUSE)

    def test_prose_before_json_still_parsed(self) -> None:
        raw = "Here you go:\n" + json.dumps(self._valid_dict())
        p = _parse_and_repair(raw)
        self.assertIsNotNone(p)

    def test_invalid_option_repaired(self) -> None:
        d = self._valid_dict()
        d["option"] = "unknown-option"
        raw = json.dumps(d)
        p = _parse_and_repair(raw)
        # Repair replaces with "new" default.
        self.assertIsNotNone(p)

    def test_missing_fields_repaired(self) -> None:
        raw = json.dumps({"option": OPTION_REUSE})
        p = _parse_and_repair(raw)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.bottles, [])

    def test_not_json_returns_none(self) -> None:
        self.assertIsNone(_parse_and_repair("totally invalid"))

    def test_stack_additions_proposal(self) -> None:
        d = {
            "option": OPTION_STACK_ADDITIONS,
            "bottles": ["basic"],
            "new_bottle": None,
            "additions": {
                "egress": [{"host": "api.fly.io", "methods": ["POST"], "why": "flyctl"}],
                "repos": [],
                "env": [],
                "credentials": [],
            },
            "similar": [],
            "notes": [],
        }
        p = _parse_and_repair(json.dumps(d))
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.additions.egress[0].host, "api.fly.io")


class TestArchitectFallback(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)
        self.cwd = self.home / "project"
        self.cwd.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                           "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_no_harness_falls_back_to_rules(self) -> None:
        with mock.patch("bot_bottle.suggest.architect._try_claude", return_value=None), \
             mock.patch("bot_bottle.suggest.architect._try_codex", return_value=None):
            p = architect_suggest("do a thing", self.cwd, host_root=self.root)
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertEqual(p.bottles, ["basic"])

    def test_architect_valid_output_used(self) -> None:
        d = {
            "option": OPTION_STACK_ADDITIONS,
            "bottles": ["basic"],
            "new_bottle": None,
            "additions": {
                "egress": [{"host": "api.example.com", "methods": ["GET"], "why": ""}],
                "repos": [], "env": [], "credentials": [],
            },
            "similar": [],
            "notes": [],
        }
        with mock.patch("bot_bottle.suggest.architect._try_claude",
                        return_value=json.dumps(d)), \
             mock.patch("bot_bottle.suggest.architect._try_codex", return_value=None):
            p = architect_suggest("do a thing", self.cwd, host_root=self.root)
        self.assertEqual(p.option, OPTION_STACK_ADDITIONS)
        self.assertEqual(p.additions.egress[0].host, "api.example.com")

    def test_architect_unparseable_falls_back(self) -> None:
        with mock.patch("bot_bottle.suggest.architect._try_claude",
                        return_value="I cannot help with that."), \
             mock.patch("bot_bottle.suggest.architect._try_codex", return_value=None):
            p = architect_suggest("do a thing", self.cwd, host_root=self.root)
        # Falls back to rules (basic) and adds a note.
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertTrue(any("fell back" in n.lower() for n in p.notes))


class TestArchitectManifest(unittest.TestCase):
    def test_manifest_has_required_fields(self) -> None:
        self.assertIn("forward_host_credentials", ARCHITECT_MANIFEST)
        self.assertIn("harness: claude", ARCHITECT_MANIFEST)
        self.assertIn('"option"', ARCHITECT_MANIFEST)

    def test_manifest_parses_as_frontmatter(self) -> None:
        from bot_bottle.contract.yaml_subset import parse_frontmatter
        fm, body = parse_frontmatter(ARCHITECT_MANIFEST)
        self.assertIn("agent_provider", fm)
        self.assertTrue(body.strip())


if __name__ == "__main__":
    unittest.main()
