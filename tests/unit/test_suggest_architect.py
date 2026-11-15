"""Tests for bottle-architect (PRD 0117): the architect on its provider
(any harness: its bottle contains it), what it's asked, and parsing its
answer.

The architect's bottle is faked by the `run` callable it's given, so no
bottle boots and no real CLI runs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.suggest.architect import (
    Architect,
    ArchitectError,
    _extract_json,
    _parse_and_repair,
    architect_suggest,
    load_architect,
)
from bot_bottle.suggest.architect_agent import PROMPT
from bot_bottle.suggest.architect_provider import Resolved
from bot_bottle.suggest.proposal import OPTION_REUSE, OPTION_STACK_ADDITIONS


def write_agent(root: Path, frontmatter: str, prompt: str = "You are the architect.") -> Path:
    path = root / "agents" / "bottle-architect.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{prompt}\n", encoding="utf-8")
    return path


def _ready(provider: str) -> Resolved:
    harness = "claude" if provider == "auto" else provider
    return Resolved(provider, harness, {"harness": harness, "login": "host"})


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

    def _valid_dict(self) -> dict[str, object]:
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


class TestLoadArchitect(unittest.TestCase):
    def test_its_prompt_is_bot_bottles_on_the_provider_resolved(self) -> None:
        with mock.patch("bot_bottle.suggest.architect.resolve", side_effect=_ready):
            got = load_architect("codex")
        self.assertEqual(("codex", "codex", PROMPT), (got.provider, got.harness, got.prompt))
        self.assertIn('"option"', got.prompt)
        text = got.agent_file()
        self.assertIn("harness: codex", text)
        self.assertIn("login: host", text)
        self.assertIn("You are bottle-architect", text)

    def test_a_connection_not_ready_or_invalid_is_an_error_saying_why(self) -> None:
        with mock.patch("bot_bottle.suggest.architect.resolve",
                        return_value=Resolved("claude", reason="claude isn't logged in")), \
             self.assertRaisesRegex(ArchitectError, "claude isn't logged in; "
                                                    "bottle-architect.connection is claude"):
            load_architect("claude")
        with self.assertRaisesRegex(ArchitectError, "isn't a bottle-architect.connection value"):
            load_architect("off")

    def test_a_host_agent_file_is_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_agent(Path(tmp), "agent_provider: pi\n", "my own prompt")
            with mock.patch("bot_bottle.suggest.architect.resolve", side_effect=_ready):
                got = load_architect("auto")
        self.assertEqual(("auto", "claude"), (got.provider, got.harness))
        self.assertNotIn("my own prompt", got.prompt)


class TestArchitectSuggest(unittest.TestCase):
    """`architect_suggest` asks its runner (the warm bottle) and parses the
    answer; the runner gets the loaded agent and the prompt."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)

    def _ask(self, answer: str | BaseException, cwd: Path | None = None,
             ) -> dict[str, object]:
        seen: dict[str, object] = {}

        def run(arch: Architect, prompt: str) -> str:
            seen["architect"], seen["prompt"] = arch, prompt
            if isinstance(answer, BaseException):
                raise answer
            return answer

        try:
            with mock.patch("bot_bottle.suggest.architect.resolve", side_effect=_ready):
                seen["proposal"] = architect_suggest("task", cwd, run=run, provider="codex",
                                                     host_root=self.root)
        except ArchitectError as e:
            seen["error"] = str(e)
        return seen

    def test_the_runner_gets_the_agent_and_the_prompt(self) -> None:
        seen = self._ask('{"option": "reuse"}')
        arch = seen["architect"]
        assert isinstance(arch, Architect)
        self.assertEqual(("codex", PROMPT), (arch.harness, arch.prompt))
        self.assertIsNotNone(seen.get("proposal"))
        self.assertIn("Respond with a single JSON object", str(seen["prompt"]))

    def test_an_answer_that_is_not_a_proposal_or_a_failed_run_is_an_error(self) -> None:
        self.assertIn("wasn't a proposal", str(self._ask("I can't help.").get("error")))
        self.assertIn("boom", str(self._ask(ArchitectError("boom")).get("error")))

    def test_with_a_project_the_model_sees_its_file_names_not_contents(self) -> None:
        project = self.root.parent / "project"
        project.mkdir()
        (project / "package.json").write_text('{"secret": "do-not-send"}', encoding="utf-8")
        prompt = str(self._ask('{"option": "reuse"}', project)["prompt"])
        self.assertIn('"package.json"', prompt)
        self.assertNotIn("do-not-send", prompt)
        self.assertNotIn("no project directory", prompt)

    def test_without_a_project_the_model_is_told_so(self) -> None:
        self.assertIn("no project directory was given",
                      str(self._ask('{"option": "reuse"}')["prompt"]))


if __name__ == "__main__":
    unittest.main()
