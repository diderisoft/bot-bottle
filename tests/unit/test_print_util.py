"""Unit: shared preflight print helpers."""

from __future__ import annotations

import unittest

from bot_bottle.backend.print_util import visible_agent_env_names


class TestVisibleAgentEnvNames(unittest.TestCase):
    def test_hides_codex_auth_placeholder(self):
        self.assertEqual(
            ["CUSTOM"],
            visible_agent_env_names(
                ["OPENAI_API_KEY", "CUSTOM"],
                agent_provider_template="codex",
            ),
        )

    def test_hides_only_active_provider_placeholder(self):
        self.assertEqual(
            ["CUSTOM", "OPENAI_API_KEY"],
            visible_agent_env_names(
                ["CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CUSTOM"],
                agent_provider_template="claude",
            ),
        )


if __name__ == "__main__":
    unittest.main()
