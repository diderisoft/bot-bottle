"""Unit: an agent's `remote` block survives loading from its markdown file."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.manifest.index import ManifestIndex


def _home(agent_frontmatter: str) -> tempfile.TemporaryDirectory[str]:
    tmp = tempfile.TemporaryDirectory()
    home = Path(tmp.name)
    (home / "agents").mkdir()
    (home / "bottles").mkdir()
    (home / "bottles" / "dev.md").write_text("---\n---\n")
    (home / "agents" / "demo.md").write_text(
        f"---\nagent_provider: pi\n{agent_frontmatter}---\n\nHi.\n")
    return tmp


class TestAgentRemoteFromMarkdown(unittest.TestCase):
    def load(self, frontmatter: str) -> bool:
        with _home(frontmatter) as home:
            index = ManifestIndex.from_md_dirs(Path(home), None)
            return index.load_for_agent("demo").agent.remote_writable

    def test_writable_true_is_kept(self):
        self.assertTrue(self.load("remote:\n  writable: true\n"))

    def test_default_is_not_writable(self):
        self.assertFalse(self.load(""))


if __name__ == "__main__":
    unittest.main()
