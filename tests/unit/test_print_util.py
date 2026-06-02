"""Unit: shared preflight print helpers."""

from __future__ import annotations

import unittest

from bot_bottle.backend.print_util import visible_agent_env_names


class TestVisibleAgentEnvNames(unittest.TestCase):
    def test_shows_all_when_no_hidden_names(self):
        self.assertEqual(
            ["CUSTOM", "OPENAI_API_KEY"],
            visible_agent_env_names(
                ["OPENAI_API_KEY", "CUSTOM"],
                hidden_env_names=frozenset(),
            ),
        )

    def test_hides_provider_placeholder(self):
        self.assertEqual(
            ["CUSTOM", "OPENAI_API_KEY"],
            visible_agent_env_names(
                ["CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CUSTOM"],
                hidden_env_names=frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}),
            ),
        )


if __name__ == "__main__":
    unittest.main()
