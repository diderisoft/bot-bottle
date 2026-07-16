"""Unit tests for the docker-free Firecracker agent-image builder.

The VM boot / SSH / buildah plumbing (`_build_in_infra`) is integration-tested
on a KVM host; here we cover the cache decision, the boot-bit injection, and
the smoke-test no-op — the logic that must hold without a VM.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.firecracker import image_builder


class TestBuildAgentRootfsDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self.dockerfile = self.cache / "Dockerfile"
        self.dockerfile.write_text("FROM node:22-slim\n")
        self.addCleanup(self._tmp.cleanup)

    def test_cache_hit_skips_rebuild(self):
        digest = image_builder._dockerfile_hash(self.dockerfile)
        base = self.cache / "rootfs" / f"agent-{digest}"
        base.mkdir(parents=True)
        (base / ".bb-ready").write_text("ok\n")
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_build_in_infra") as build:
            out = image_builder.build_agent_rootfs_dir(
                self.dockerfile, image_tag="t:latest")
        build.assert_not_called()
        self.assertEqual(base, out)

    def test_cache_miss_builds_injects_and_marks_ready(self):
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_build_in_infra") as build, \
             patch.object(image_builder.util, "inject_guest_boot") as inject:
            out = image_builder.build_agent_rootfs_dir(
                self.dockerfile, image_tag="t:latest", smoke_test=("claude", "--version"))
        build.assert_called_once()
        # smoke_test threads through to the VM build.
        self.assertEqual(("claude", "--version"), build.call_args.args[2])
        inject.assert_called_once_with(out)
        self.assertTrue((out / ".bb-ready").is_file())

    def test_content_addressed_cache_key(self):
        other = self.cache / "Dockerfile2"
        other.write_text("FROM python:3.12-slim\n")
        self.assertNotEqual(
            image_builder._dockerfile_hash(self.dockerfile),
            image_builder._dockerfile_hash(other),
        )


class TestSmokeTest(unittest.TestCase):
    def test_empty_argv_is_noop(self):
        with patch.object(image_builder, "_ssh") as ssh:
            image_builder._smoke_test(Path("/k"), "10.0.0.1", "tag", ())
        ssh.assert_not_called()

    def test_failed_smoke_dies(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, stdout="broken", stderr="")
        with patch.object(image_builder, "_ssh", return_value=result), \
             self.assertRaises(SystemExit):
            image_builder._smoke_test(Path("/k"), "10.0.0.1", "tag", ("claude", "--version"))


if __name__ == "__main__":
    unittest.main()
