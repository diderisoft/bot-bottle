"""Unit tests for the immutable container-build input policy."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

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

    def test_rejects_missing_dynamic_and_malformed_bases(self):
        cases = (
            ("RUN true\n", "missing FROM"),
            ("FROM ${BASE_IMAGE}\n", "dynamic base"),
            (
                "ARG ORCHESTRATOR_BASE_IMAGE=base:latest\n"
                "FROM ${ORCHESTRATOR_BASE_IMAGE}\n",
                "mutable default",
            ),
            ("FROM node:22@sha256:abc\n", "64 lowercase hex"),
        )
        for text, expected in cases:
            with self.subTest(expected=expected):
                problems = policy.check_dockerfile(Path("Dockerfile"), text)
                self.assertTrue(any(expected in item for item in problems))

    def test_requires_gateway_lock_and_verified_codex_markers(self):
        base = "FROM node:22.23.1@sha256:" + "a" * 64 + "\n"
        gateway = policy.check_dockerfile(Path("Dockerfile.gateway"), base)
        self.assertTrue(any("require hashes" in item for item in gateway))
        self.assertTrue(any("lock is not consumed" in item for item in gateway))

        codex = policy.check_dockerfile(
            Path("bot_bottle/contrib/codex/Dockerfile"),
            base,
        )
        self.assertEqual(3, len(codex))
        self.assertTrue(all("verified pinned Codex install" in item for item in codex))


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

    def test_rejects_unreadable_stale_or_old_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = policy.check_npm_manifest(root, Path("missing/package.json"))
            self.assertTrue(any("cannot read" in item for item in missing))

            path = self._write(root, version="1.2.3", integrity="sha512-abc")
            lock_path = root / path.with_name("package-lock.json")
            lock = json.loads(lock_path.read_text())
            lock["lockfileVersion"] = 2
            lock["packages"][""]["dependencies"] = {}
            lock_path.write_text(json.dumps(lock))
            problems = policy.check_npm_manifest(root, path)
            self.assertTrue(any("root dependencies are stale" in item for item in problems))
            self.assertTrue(any("lockfileVersion 3" in item for item in problems))


class TestPythonLockPolicy(unittest.TestCase):
    def test_rejects_missing_or_empty_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = policy.check_python_lock(root)
            self.assertTrue(any("cannot read lock" in item for item in missing))

            (root / "requirements.gateway.lock").write_text("# empty\n")
            problems = policy.check_python_lock(root)
            self.assertTrue(any("contains no exact requirements" in item for item in problems))
            self.assertTrue(any("cannot read input" in item for item in problems))

    def test_rejects_unhashed_and_stale_direct_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.gateway.lock").write_text(
                "example== \\\n"
                "    # deliberately malformed and unhashed\n"
            )
            (root / "requirements.gateway.in").write_text(
                "example>=1\n"
                "missing==2.0.0\n"
            )
            problems = policy.check_python_lock(root)
            self.assertTrue(any("example is not exact" in item for item in problems))
            self.assertTrue(any("example lacks a sha256 hash" in item for item in problems))
            self.assertTrue(any("direct dependency is not exact" in item for item in problems))
            self.assertTrue(any("missing direct dependency" in item for item in problems))


class TestRepositoryPolicy(unittest.TestCase):
    def test_repository_image_inputs_pass(self):
        self.assertEqual([], policy.check_repo())

    def test_missing_repository_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            policy,
            "DOCKERFILES",
            (Path("missing.Dockerfile"),),
        ), patch.object(policy, "NPM_MANIFESTS", ()):
            problems = policy.check_repo(Path(directory))
        self.assertTrue(any("cannot read Dockerfile" in item for item in problems))

    def test_main_reports_success_and_failure(self):
        with patch.object(policy, "check_repo", return_value=[]), redirect_stdout(io.StringIO()):
            self.assertEqual(0, policy.main())
        stderr = io.StringIO()
        with patch.object(
            policy,
            "check_repo",
            return_value=["bad base"],
        ), redirect_stderr(stderr):
            self.assertEqual(1, policy.main())
        self.assertIn("bad base", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
