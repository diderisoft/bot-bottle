"""Unit: session-end state capture (crash preservation), state settling,
and the agent arguments a launch runs.

The launch-context machinery is covered by integration; this isolates
the post-session decision: mark for preservation if non-zero exit,
no-op for clean exit."""

import tempfile
import unittest
from pathlib import Path

from tests.unit import use_bottle_root
from bot_bottle import bottle_state
from bot_bottle.control.hostctl.bottles import agent_args


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cli-start-settle.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")

    def _teardown_fake_home(self):
        self._restore()
        self._tmp.cleanup()


class TestCaptureSessionState(_FakeHomeMixin, unittest.TestCase):
    # capture_claude_session_state handles the preserve marker for
    # non-zero agent exits.
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_clean_exit_does_not_mark(self):
        bottle_state.capture_claude_session_state("dev-abc", exit_code=0)
        self.assertFalse(bottle_state.is_preserved("dev-abc"))

    def test_crash_marks_preserved(self):
        bottle_state.capture_claude_session_state("dev-abc", exit_code=137)
        self.assertTrue(bottle_state.is_preserved("dev-abc"))

    def test_ctrl_c_treated_as_crash(self):
        # SIGINT delivers exit 130; the operator may have Ctrl-C'd
        # because something went wrong, so we preserve.
        bottle_state.capture_claude_session_state("dev-abc", exit_code=130)
        self.assertTrue(bottle_state.is_preserved("dev-abc"))

    def test_empty_identity_is_noop(self):
        # Backends without an identity field shouldn't crash this
        # path (the _identity_from_plan helper falls back to "").
        bottle_state.capture_claude_session_state("", exit_code=137)
        self.assertFalse(bottle_state.is_preserved(""))


class TestSettleState(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_preserved_state_survives(self):
        bottle_state.write_per_bottle_dockerfile("dev-abc", "FROM x\n")
        bottle_state.mark_preserved("dev-abc")
        bottle_state.settle_state("dev-abc")
        self.assertTrue(bottle_state.bottle_state_dir("dev-abc").is_dir())

    def test_unpreserved_state_is_cleaned(self):
        bottle_state.write_per_bottle_dockerfile("dev-abc", "FROM x\n")
        bottle_state.settle_state("dev-abc")
        self.assertFalse(bottle_state.bottle_state_dir("dev-abc").exists())

    def test_empty_identity_is_noop(self):
        bottle_state.settle_state("")  # should not raise


class TestAgentArgs(unittest.TestCase):
    def test_passes_provider_startup_args(self):
        self.assertEqual(
            ["--models", "openrouter/google/gemma"],
            agent_args("pi", ("--models", "openrouter/google/gemma"), ""),
        )

    def test_remote_control_is_provider_startup_arg(self):
        self.assertEqual(
            ["--dangerously-bypass-approvals-and-sandbox", "remote-control"],
            agent_args("codex", ("remote-control",), ""),
        )

    def test_prompt_follows_the_startup_args(self):
        args = agent_args("claude", ("--x",), "Do it")
        self.assertEqual("--x", args[args.index("--x")])
        self.assertIn("Do it", args[args.index("--x") + 1:])

    def test_headless_prompt_runs_to_completion(self):
        self.assertEqual(["-p", "Do it"], agent_args("claude", (), "Do it")[-2:])

    def test_interactive_prompt_is_the_first_message(self):
        # A console launch: the session stays open after the first turn.
        for template in ("claude", "pi", "codex"):
            with self.subTest(template=template):
                args = agent_args(template, (), "Do it", headless=False)
                self.assertEqual("Do it", args[-1])
                self.assertNotIn("-p", args)

    def test_a_console_session_gets_the_providers_remote_view_args(self):
        self.assertIn("--bb-remote-view=true", agent_args("pi", (), "Do it", headless=False,
                                                     remote_view=True))
        self.assertNotIn("--bb-remote-view=true", agent_args("pi", (), "Do it", headless=False))
        # The prompt stays its own argument: a bare flag would take it as its value.
        args = agent_args("pi", (), "Do it", headless=False, remote_view=True)
        self.assertEqual("Do it", args[-1])
        self.assertTrue(all("=" in a for a in args if a.startswith("--bb-")))
        self.assertEqual(agent_args("claude", (), "", remote_view=True), agent_args("claude", (), ""))

    def test_a_prompt_that_looks_like_an_option_stays_a_prompt(self):
        for template in ("claude", "pi", "codex"):
            for prompt in ("-v is broken, fix it", "@README.md says otherwise"):
                with self.subTest(template=template, prompt=prompt):
                    self.assertEqual(" " + prompt,
                                     agent_args(template, (), prompt, headless=False)[-1])

    def test_an_agent_without_interactive_prompts_is_refused(self):
        from unittest import mock

        from bot_bottle.control.hostctl.bottles import PromptUnsupported

        provider = mock.Mock()
        provider.interactive_prompt.return_value = None
        with mock.patch("bot_bottle.agent_provider.get_provider", return_value=provider), \
             mock.patch("bot_bottle.agent_provider.runtime_for",
                        return_value=mock.Mock(bypass_args=())):
            with self.assertRaises(PromptUnsupported):
                agent_args("other", (), "Do it", headless=False)


if __name__ == "__main__":
    unittest.main()
