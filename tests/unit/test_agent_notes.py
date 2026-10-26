"""The fixed operating notes bot-bottle adds to every agent's prompt (ADR 0003)."""

import unittest

from bot_bottle.contract.agent_notes import GIT_BOUNDARY, OPERATING_NOTES, with_operating_notes


class TestOperatingNotes(unittest.TestCase):
    def test_appended_after_the_operator_prompt(self):
        out = with_operating_notes("Be terse.\n\n")
        self.assertTrue(out.startswith("Be terse.\n\n## Working in a bot-bottle sandbox"))
        self.assertIn(GIT_BOUNDARY, out)

    def test_alone_without_a_prompt(self):
        self.assertEqual(OPERATING_NOTES, with_operating_notes(""))
        self.assertEqual(OPERATING_NOTES, with_operating_notes(None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
