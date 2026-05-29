"""Unit: provider runtime defaults."""

from __future__ import annotations

import unittest

from bot_bottle.agent_provider import runtime_for


class TestAgentProviderRuntime(unittest.TestCase):
    def test_claude_keeps_oauth_placeholder(self):
        runtime = runtime_for("claude")
        self.assertEqual("claude_code_oauth", runtime.auth_role)
        self.assertEqual("CLAUDE_CODE_OAUTH_TOKEN", runtime.placeholder_env)

    def test_codex_does_not_inject_openai_api_key_placeholder(self):
        runtime = runtime_for("codex")
        self.assertEqual("", runtime.auth_role)
        self.assertEqual("", runtime.placeholder_env)


if __name__ == "__main__":
    unittest.main()
