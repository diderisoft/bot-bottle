"""Unit contracts shared by all built-in agent images."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRIB_DIR = _REPO_ROOT / "bot_bottle/contrib"
_AGENT_DOCKERFILES = tuple(sorted(_CONTRIB_DIR.glob("*/Dockerfile")))


class TestBuiltinAgentImages(unittest.TestCase):
    def test_all_share_one_digest_pinned_node_trixie_base(self):
        self.assertTrue(_AGENT_DOCKERFILES)
        inputs = json.loads((_REPO_ROOT / "image-build-args.json").read_text())
        self.assertRegex(
            inputs["NODE_BASE_IMAGE"],
            r"^node:22\.\d+\.\d+-trixie-slim@sha256:[0-9a-f]{64}$",
        )
        for dockerfile in _AGENT_DOCKERFILES:
            with self.subTest(provider=dockerfile.parent.name):
                text = dockerfile.read_text()
                self.assertRegex(text, r"(?m)^ARG NODE_BASE_IMAGE$")
                self.assertIn("FROM ${NODE_BASE_IMAGE}", text)

    def test_orchestrator_and_gateway_share_configurable_python_base(self):
        inputs = json.loads((_REPO_ROOT / "image-build-args.json").read_text())
        self.assertRegex(
            inputs["PYTHON_BASE_IMAGE"],
            r"^python:3\.12\.\d+-slim-trixie@sha256:[0-9a-f]{64}$",
        )
        for name in ("Dockerfile.orchestrator", "Dockerfile.gateway"):
            dockerfile = _REPO_ROOT / name
            text = dockerfile.read_text()
            with self.subTest(dockerfile=name):
                self.assertRegex(text, r"(?m)^ARG PYTHON_BASE_IMAGE$")
                self.assertIn("FROM ${PYTHON_BASE_IMAGE}", text)

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
