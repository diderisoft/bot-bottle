"""Unit contracts shared by all built-in agent images."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_CONTRIB_DIR = Path(__file__).resolve().parents[2] / "bot_bottle/contrib"
_AGENT_DOCKERFILES = tuple(sorted(_CONTRIB_DIR.glob("*/Dockerfile")))


class TestBuiltinAgentImages(unittest.TestCase):
    def test_all_share_one_digest_pinned_node_trixie_base(self):
        self.assertTrue(_AGENT_DOCKERFILES)
        bases = []
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                match = re.search(
                    r"(?m)^FROM "
                    r"(node:22\.\d+\.\d+-trixie-slim@sha256:[0-9a-f]{64})\s*$",
                    dockerfile.read_text(),
                )
                if match is None:
                    self.fail(f"{dockerfile} does not use a digest-pinned Node base")
                bases.append(match.group(1))
        self.assertEqual(1, len(set(bases)))

    def test_none_install_podman(self):
        # podman lives in the nested-containers derived layer (nested_containers.py),
        # not in the base agent images, so bottles without the flag pay no cost.
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                self.assertNotRegex(
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
