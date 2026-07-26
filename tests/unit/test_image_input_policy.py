"""Unit tests for the immutable container-build input policy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import check_image_inputs as policy


class TestDockerfilePolicy(unittest.TestCase):
    def test_accepts_versioned_digest_base(self):
        text = "FROM node:22.23.1-trixie-slim@sha256:" + "a" * 64 + "\n"
        self.assertEqual([], policy.check_dockerfile(Path("Dockerfile"), text))

    def test_rejects_mutable_and_latest_bases(self):
        for value in (
            "node:22-trixie-slim",
            "node:latest@sha256:" + "a" * 64,
            "node@sha256:" + "a" * 64,
        ):
            with self.subTest(value=value):
                problems = policy.check_dockerfile(
                    Path("Dockerfile"), f"FROM {value}\n",
                )
                self.assertTrue(problems)

    def test_rejects_network_pipe_to_shell(self):
        text = (
            "FROM node:22.23.1@sha256:" + "a" * 64
            + "\nRUN curl -fsSL https://example.invalid/install.sh | sh\n"
        )
        problems = policy.check_dockerfile(Path("Dockerfile"), text)
        self.assertTrue(any("directly into a shell" in item for item in problems))

    def test_rejects_unlocked_npm_and_pi_installs(self):
        base = "FROM node:22.23.1@sha256:" + "a" * 64 + "\nRUN "
        for install in (
            "npm install -g package@1.2.3",
            "pi install npm:extension@1.2.3",
        ):
            with self.subTest(install=install):
                problems = policy.check_dockerfile(
                    Path("Dockerfile"), base + install + "\n",
                )
                self.assertTrue(any("npm ci" in item for item in problems))

    def test_requires_snapshot_for_apt(self):
        text = (
            "FROM node:22.23.1@sha256:" + "a" * 64
            + "\nRUN apt-get update && apt-get install git\n"
        )
        problems = policy.check_dockerfile(Path("Dockerfile"), text)
        self.assertTrue(any("fixed Debian snapshot" in item for item in problems))


class TestNpmLockPolicy(unittest.TestCase):
    def _write(self, root: Path, *, version: str, integrity: str = "") -> Path:
        package_dir = root / "provider"
        package_dir.mkdir()
        manifest = {
            "name": "test",
            "private": True,
            "dependencies": {"example": version},
        }
        package = {
            "version": "1.2.3",
            "resolved": "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        }
        if integrity:
            package["integrity"] = integrity
        lock = {
            "name": "test",
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"example": version}},
                "node_modules/example": package,
            },
        }
        (package_dir / "package.json").write_text(json.dumps(manifest))
        (package_dir / "package-lock.json").write_text(json.dumps(lock))
        return Path("provider/package.json")

    def test_accepts_exact_integrity_locked_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write(root, version="1.2.3", integrity="sha512-abc")
            self.assertEqual([], policy.check_npm_manifest(root, path))

    def test_rejects_range_and_missing_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write(root, version="^1.2.3")
            problems = policy.check_npm_manifest(root, path)
            self.assertTrue(any("not an exact version" in item for item in problems))
            self.assertTrue(any("lacks sha512 integrity" in item for item in problems))


class TestRepositoryPolicy(unittest.TestCase):
    def test_repository_image_inputs_pass(self):
        self.assertEqual([], policy.check_repo())


if __name__ == "__main__":
    unittest.main()
