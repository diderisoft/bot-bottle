"""Tests for bottle-architect (PRD 0117): loading the host's agent, refusing
harnesses that can't run with no tools, the tool-less claude call, and
parsing its answer.

The harness call is faked by replacing `subprocess.run`, so no real CLI runs.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.suggest import architect
from bot_bottle.suggest.architect import (
    ArchitectError,
    _extract_json,
    _parse_and_repair,
    architect_suggest,
    load_architect,
)
from bot_bottle.suggest.proposal import OPTION_REUSE, OPTION_STACK_ADDITIONS
from bot_bottle.suggest.starter import ARCHITECT_STARTER


def write_agent(root: Path, frontmatter: str, prompt: str = "You are the architect.") -> Path:
    path = root / "agents" / "bottle-architect.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{prompt}\n", encoding="utf-8")
    return path


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
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_reads_the_prompt_harness_and_model_from_the_agent_file(self) -> None:
        path = write_agent(self.root, "model: claude-sonnet-5\nagent_provider: claude\n",
                           "Propose tiny bottles.")
        got = load_architect(self.root)
        self.assertEqual((path, "Propose tiny bottles.", "claude", "claude-sonnet-5"),
                         (got.path, got.prompt, got.harness, got.model))

    def test_the_setup_starter_loads(self) -> None:
        path = self.root / "agents" / "bottle-architect.md"
        path.parent.mkdir(parents=True)
        path.write_text(ARCHITECT_STARTER, encoding="utf-8")
        got = load_architect(self.root)
        self.assertEqual(("claude", ""), (got.harness, got.model))
        self.assertIn('"option"', got.prompt)

    def test_a_harness_that_cannot_run_tool_less_is_refused(self) -> None:
        for harness, why in (("codex", "shell"), ("pi", "no verified way"),
                             ("my-harness", "no tools")):
            with self.subTest(harness=harness):
                write_agent(self.root, f"agent_provider:\n  harness: {harness}\n")
                with self.assertRaises(ArchitectError) as cm:
                    load_architect(self.root)
                self.assertIn(f"harness {harness!r} can't run", str(cm.exception))
                self.assertIn(why, str(cm.exception))

    def test_missing_unloadable_promptless_or_odd_model_is_an_error(self) -> None:
        with self.assertRaisesRegex(ArchitectError, "bot-bottle setup"):
            load_architect(self.root)
        cases = (("agent_provider:\n  harness: claude\n  bogus: 1\n", "x", "doesn't load"),
                 ("agent_provider: claude\n", "", "no prompt"),
                 ("model: --dangerous\n", "x", "plain model name"))
        for frontmatter, prompt, why in cases:
            with self.subTest(why=why):
                write_agent(self.root, frontmatter, prompt)
                with self.assertRaisesRegex(ArchitectError, why):
                    load_architect(self.root)


class ClaudeInvocation(unittest.TestCase):
    """The harness runs with no tools, no MCP, no settings and no saved
    session, from an empty directory: it can only answer."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)

    def _run(self, frontmatter: str, *, result: object) -> dict[str, object]:
        write_agent(self.root, frontmatter, "The agent file's prompt.")
        seen: dict[str, object] = {}

        def run(argv, **kw):  # type: ignore[no-untyped-def]
            seen["argv"], seen["timeout"], seen["input"] = argv, kw.get("timeout"), kw.get("input")
            seen["listing"] = os.listdir(kw["cwd"])
            if isinstance(result, BaseException):
                raise result
            return result

        with mock.patch.object(architect.shutil, "which", return_value="/bin/claude"), \
             mock.patch.object(architect.subprocess, "run", side_effect=run):
            try:
                seen["proposal"] = architect_suggest("task", None, host_root=self.root)
            except ArchitectError as e:
                seen["error"] = str(e)
        return seen

    def test_no_tools_from_an_empty_directory_with_the_agents_prompt_and_model(self) -> None:
        seen = self._run("model: claude-sonnet-5\n",
                         result=mock.Mock(returncode=0, stdout='{"option": "reuse"}'))
        argv = seen["argv"]
        assert isinstance(argv, list)
        self.assertEqual(["/bin/claude", *architect.CLAUDE_ARGS, "--system-prompt",
                          "The agent file's prompt.", "--model", "claude-sonnet-5"], argv)
        self.assertEqual("", argv[argv.index("--tools") + 1])
        self.assertEqual("", argv[argv.index("--setting-sources") + 1])
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual([], seen["listing"])
        self.assertEqual(architect.TIMEOUT_SECONDS, seen["timeout"])
        self.assertIsNotNone(seen.get("proposal"))

    def test_no_model_line_means_no_model_flag(self) -> None:
        seen = self._run("agent_provider: claude\n",
                         result=mock.Mock(returncode=0, stdout='{"option": "reuse"}'))
        argv = seen["argv"]
        assert isinstance(argv, list)
        self.assertNotIn("--model", argv)

    def test_failures_are_errors_with_a_reason(self) -> None:
        import subprocess  # pylint: disable=import-outside-toplevel
        cases = ((mock.Mock(returncode=1, stdout=""), "logged in"),
                 (subprocess.TimeoutExpired("claude", 120), "didn't answer within"),
                 (mock.Mock(returncode=0, stdout="I can't help."), "wasn't a proposal"))
        for result, why in cases:
            with self.subTest(why=why):
                self.assertIn(why, str(self._run("agent_provider: claude\n", result=result)
                                       .get("error")))

    def test_with_a_project_the_model_sees_its_file_names_not_contents(self) -> None:
        write_agent(self.root, "agent_provider: claude\n")
        project = self.root.parent / "project"
        project.mkdir()
        (project / "package.json").write_text('{"secret": "do-not-send"}', encoding="utf-8")
        seen: dict[str, str] = {}

        def run(argv, **kw):  # type: ignore[no-untyped-def]
            del argv
            seen["input"] = kw["input"]
            return mock.Mock(returncode=0, stdout='{"option": "reuse"}')

        with mock.patch.object(architect.shutil, "which", return_value="/bin/claude"), \
             mock.patch.object(architect.subprocess, "run", side_effect=run):
            architect_suggest("task", project, host_root=self.root)
        self.assertIn('"package.json"', seen["input"])
        self.assertNotIn("do-not-send", seen["input"])
        self.assertNotIn("no project directory", seen["input"])

    def test_claude_that_wont_start_is_an_error(self) -> None:
        self.assertIn("didn't run", str(self._run("agent_provider: claude\n",
                                                  result=OSError("exec format error"))
                                        .get("error")))

    def test_claude_not_installed(self) -> None:
        write_agent(self.root, "agent_provider: claude\n")
        with mock.patch.object(architect.shutil, "which", return_value=None), \
             self.assertRaisesRegex(ArchitectError, "isn't installed"):
            architect_suggest("task", None, host_root=self.root)

    def test_without_a_project_the_model_is_told_so(self) -> None:
        seen = self._run("agent_provider: claude\n",
                         result=mock.Mock(returncode=0, stdout='{"option": "reuse"}'))
        self.assertIn("no project directory was given", str(seen["input"]))


if __name__ == "__main__":
    unittest.main()
