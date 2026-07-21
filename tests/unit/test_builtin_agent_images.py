"""Unit contracts shared by all built-in agent images."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_CONTRIB_DIR = Path(__file__).resolve().parents[2] / "bot_bottle/contrib"
_AGENT_DOCKERFILES = tuple(sorted(_CONTRIB_DIR.glob("*/Dockerfile")))


class TestBuiltinAgentImages(unittest.TestCase):
    def test_all_use_debian_trixie_stable(self):
        self.assertTrue(_AGENT_DOCKERFILES)
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                self.assertRegex(
                    dockerfile.read_text(),
                    r"(?m)^FROM node:22-trixie-slim\s*$",
                )

    def test_all_install_podman(self):
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                self.assertRegex(
                    dockerfile.read_text(),
                    re.compile(r"(?m)^\s*podman(?:\s|\\|$)"),
                )


if __name__ == "__main__":
    unittest.main()
