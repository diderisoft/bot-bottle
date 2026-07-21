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
                    r"(?m)^FROM debian:trixie-slim\s*$",
                )

    def test_all_create_node_runtime_user(self):
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                dockerfile_text = dockerfile.read_text()
                self.assertIn("groupadd --gid 1000 node", dockerfile_text)
                self.assertIn(
                    "useradd --uid 1000 --gid node --shell /bin/bash "
                    "--create-home node",
                    dockerfile_text,
                )

    def test_all_install_podman(self):
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                self.assertRegex(
                    dockerfile.read_text(),
                    re.compile(r"(?m)^\s*podman(?:\s|\\|$)"),
                )

    def test_all_install_ssh_client(self):
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                self.assertRegex(
                    dockerfile.read_text(),
                    re.compile(r"(?m)^\s*openssh-client(?:\s|\\|$)"),
                )

    def test_all_prepare_node_git_config_directory(self):
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                dockerfile_text = dockerfile.read_text()
                self.assertIn("install -d -o node -g node", dockerfile_text)
                self.assertIn("/home/node/.config/git", dockerfile_text)


if __name__ == "__main__":
    unittest.main()
