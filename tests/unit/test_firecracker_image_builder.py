"""Unit tests for the docker-free Firecracker agent-image builder.

The VM boot / SSH / buildah plumbing (`_build_in_infra`) is integration-tested
on a KVM host; here we cover the cache decision, the boot-bit injection, and
the smoke-test no-op — the logic that must hold without a VM.
"""

from __future__ import annotations

import subprocess
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
        digest = image_builder._rootfs_digest(self.dockerfile)
        base = self.cache / "rootfs" / f"agent-{digest}"
        base.mkdir(parents=True)
        (base / ".bb-ready").write_text("ok\n")
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_build_in_infra") as build:
            out = image_builder.build_agent_rootfs_dir(
                self.dockerfile, image_tag="t:latest")
        build.assert_not_called()
        self.assertEqual(base, out)

    def test_cached_lookup_requires_ready_marker(self):
        digest = image_builder._rootfs_digest(self.dockerfile)
        base = self.cache / "rootfs" / f"agent-{digest}"
        base.mkdir(parents=True)
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache):
            self.assertIsNone(image_builder.cached_agent_rootfs_dir(self.dockerfile))
            (base / ".bb-ready").write_text("ok\n")
            self.assertEqual(base, image_builder.cached_agent_rootfs_dir(self.dockerfile))

    def test_cache_miss_builds_injects_and_marks_ready(self):
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_build_in_infra") as build, \
             patch.object(image_builder.util, "inject_guest_boot") as inject:
            out = image_builder.build_agent_rootfs_dir(
                self.dockerfile, image_tag="t:latest", smoke_test=("claude", "--version"))
        build.assert_called_once()
        # smoke_test threads through to the VM build.
        self.assertEqual(("claude", "--version"), build.call_args.args[2])
        inject.assert_called_once()
        self.assertTrue((out / ".bb-ready").is_file())

    def test_pulled_image_cache_hit_skips_pull(self):
        image = "reg/o/bot-bottle-claude@sha256:" + "a" * 64
        digest = image_builder._image_rootfs_digest(image)
        base = self.cache / "rootfs" / f"agent-image-{digest}"
        base.mkdir(parents=True)
        (base / ".bb-ready").write_text("ok\n")
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_pull_in_infra") as pull:
            out = image_builder.acquire_agent_image_rootfs_dir(image)
        pull.assert_not_called()
        self.assertEqual(base, out)

    def test_pulled_image_cache_miss_pulls_injects_and_marks_ready(self):
        image = "reg/o/bot-bottle-claude@sha256:" + "b" * 64
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_pull_in_infra") as pull, \
             patch.object(image_builder.util, "inject_guest_boot") as inject:
            out = image_builder.acquire_agent_image_rootfs_dir(
                image, smoke_test=("claude", "--version"))
        pull.assert_called_once()
        self.assertEqual(("claude", "--version"), pull.call_args.args[2])
        inject.assert_called_once()
        self.assertTrue((out / ".bb-ready").is_file())

    def test_acquire_requires_a_digest_pinned_image(self):
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache):
            with self.assertRaises(SystemExit):
                image_builder.acquire_agent_image_rootfs_dir("reg/o/bot-bottle-claude:0.1.0")

    def test_pull_in_infra_pulls_smoke_tests_and_streams(self):
        # The VM SSH/buildah I/O is mocked; the orchestration logic (pull the
        # pinned image, smoke test, export rootfs, clean up) is exercised.
        from unittest.mock import MagicMock
        image = "reg/o/bot-bottle-claude@sha256:" + "c" * 64
        base = self.cache / "out"
        base.mkdir()
        svc = MagicMock()
        svc.orchestrator.return_value.ssh_target.return_value = (Path("/k"), "10.0.0.2")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(image_builder, "FirecrackerInfraService", return_value=svc), \
             patch.object(image_builder, "_ssh", return_value=ok) as ssh, \
             patch.object(image_builder, "_smoke_test") as smoke, \
             patch.object(image_builder, "_stream_rootfs") as stream:
            image_builder._pull_in_infra(image, base, ("claude", "--version"), "deadbeef")
        svc.ensure_running.assert_called_once()
        self.assertTrue(
            any("buildah pull" in c.args[2] and "@sha256:" in c.args[2]
                for c in ssh.call_args_list),
            "expected a buildah pull of the digest-pinned image",
        )
        smoke.assert_called_once()
        stream.assert_called_once()

    def test_overlay_cache_hit_skips_build(self):
        from bot_bottle.manifest import ManifestImageOverlay
        base_ref = "reg/o/bot-bottle-claude@sha256:" + "d" * 64
        overlay = ManifestImageOverlay(apt=("jq",))
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache):
            digest = image_builder._overlay_rootfs_digest(base_ref, overlay)
            base = self.cache / "rootfs" / f"agent-overlay-{digest}"
            base.mkdir(parents=True)
            (base / ".bb-ready").write_text("ok\n")
            with patch.object(image_builder, "_build_overlay_in_infra") as build:
                out = image_builder.acquire_agent_overlay_rootfs_dir(
                    base_ref, overlay, template="claude", source_root=self.cache)
        build.assert_not_called()
        self.assertEqual(base, out)

    def test_build_overlay_in_infra_logs_in_builds_and_streams(self):
        from unittest.mock import MagicMock
        from bot_bottle.manifest import ManifestImageOverlay
        base_ref = "reg/o/bot-bottle-claude@sha256:" + "c" * 64
        out = self.cache / "overlay-out"
        out.mkdir()
        svc = MagicMock()
        svc.orchestrator.return_value.ssh_target.return_value = (Path("/k"), "10.0.0.2")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(image_builder, "FirecrackerInfraService", return_value=svc), \
             patch.object(image_builder, "_ssh", return_value=ok) as ssh, \
             patch.object(image_builder, "_send_dockerfile") as send_df, \
             patch.object(image_builder, "_send_overlay_files"), \
             patch.object(image_builder, "_buildah_build") as build, \
             patch.object(image_builder, "_smoke_test"), \
             patch.object(image_builder, "_stream_rootfs") as stream, \
             patch.dict("os.environ", {image_builder._ARTIFACT_TOKEN_ENV: "TOK"}):
            image_builder._build_overlay_in_infra(
                base_ref, ManifestImageOverlay(apt=("jq",)), out,
                self.cache, ("claude", "--version"), "deadbeef",
                template="claude")
        send_df.assert_called_once()
        build.assert_called_once()
        stream.assert_called_once()
        self.assertTrue(
            any("buildah login" in c.args[2] for c in ssh.call_args_list),
            "expected a buildah login for the private base pull",
        )

    def test_content_addressed_cache_key(self):
        other = self.cache / "Dockerfile2"
        other.write_text("FROM python:3.12-slim\n")
        self.assertNotEqual(
            image_builder._dockerfile_hash(self.dockerfile),
            image_builder._dockerfile_hash(other),
        )

    def test_rootfs_digest_tracks_dockerfile_and_init(self):
        # Same Dockerfile, different injected init -> different rootfs key, so
        # an init fix (e.g. /tmp perms) rebuilds instead of reusing a stale
        # rootfs; different Dockerfiles also differ.
        base = image_builder._rootfs_digest(self.dockerfile)
        with patch.object(image_builder.util, "_GUEST_INIT", "#!/bin/sh\n# changed\n"):
            self.assertNotEqual(base, image_builder._rootfs_digest(self.dockerfile))
        other = self.cache / "Dockerfile2"
        other.write_text("FROM python:3.12-slim\n")
        self.assertNotEqual(base, image_builder._rootfs_digest(other))

    def test_rootfs_digest_tracks_centralized_build_args(self):
        with patch.object(
            image_builder.resources,
            "image_build_args",
            return_value={"NODE_BASE_IMAGE": "node:first"},
        ):
            first = image_builder._rootfs_digest(self.dockerfile)
        with patch.object(
            image_builder.resources,
            "image_build_args",
            return_value={"NODE_BASE_IMAGE": "node:second"},
        ):
            second = image_builder._rootfs_digest(self.dockerfile)
        self.assertNotEqual(first, second)


class TestBuildContext(unittest.TestCase):
    """The COPY-source parsing + context shipping that lets a Dockerfile pin an
    input by COPYing a committed file (codex checksum list, claude/pi npm
    lockfiles) through the otherwise-empty VM-side build context."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        codex = self.root / "bot_bottle" / "contrib" / "codex"
        codex.mkdir(parents=True)
        self.sums = codex / "codex-package_SHA256SUMS"
        self.sums.write_text("aaaa  codex-package-x86_64-unknown-linux-musl.tar.gz\n")
        self.dockerfile = self.root / "Dockerfile"
        self.dockerfile.write_text(
            "FROM node:22-slim\n"
            "COPY --chown=node:node "
            "bot_bottle/contrib/codex/codex-package_SHA256SUMS /tmp/x\n"
        )

    def _patch_root(self):
        return patch.object(
            image_builder.resources, "build_root", return_value=self.root)

    def test_copy_sources_parses_flags_and_continuations(self):
        df = self.root / "Multi"
        df.write_text(
            "FROM x\n"
            "COPY a/one.json \\\n     a/two.json /dest/\n"
            "COPY --from=builder /built /built\n"          # excluded: build stage
            "COPY --chown=n:n b/three /dest\n"
        )
        self.assertEqual(
            image_builder._context_copy_sources(df),
            ["a/one.json", "a/two.json", "b/three"],
        )

    def test_add_sources_are_parsed_like_copy_but_skip_remote(self):
        df = self.root / "WithAdd"
        df.write_text(
            "FROM x\n"
            "ADD --chown=n:n c/local.txt /dest/\n"           # local: included
            "ADD https://example.com/x.tar /dest/\n"         # remote URL: excluded
            "ADD git://example.com/repo /dest/\n"            # git ref: excluded
        )
        self.assertEqual(
            image_builder._context_copy_sources(df),
            ["c/local.txt"],
        )

    def test_context_files_resolves_existing_under_root(self):
        with self._patch_root():
            files = image_builder._context_files(self.dockerfile)
        self.assertEqual(
            [rel for rel, _ in files],
            ["bot_bottle/contrib/codex/codex-package_SHA256SUMS"],
        )

    def test_context_files_recurses_into_directory_sources(self):
        nested = self.root / "assets" / "nested"
        nested.mkdir(parents=True)
        (self.root / "assets" / "one").write_text("one")
        (nested / "two").write_text("two")
        df = self.root / "Directory"
        df.write_text("FROM x\nCOPY assets /opt/assets\n")
        with self._patch_root():
            files = image_builder._context_files(df)
        self.assertEqual(
            [rel for rel, _ in files],
            ["assets/nested/two", "assets/one"],
        )

    def test_context_files_drops_missing_and_traversal(self):
        df = self.root / "Bad"
        df.write_text("FROM x\nCOPY ../escape /d\nCOPY does/not/exist /d\n")
        with self._patch_root():
            self.assertEqual(image_builder._context_files(df), [])

    def test_rootfs_digest_tracks_context_file_content(self):
        with self._patch_root(), \
             patch.object(image_builder.util, "cache_dir", return_value=self.root):
            first = image_builder._rootfs_digest(self.dockerfile)
            self.sums.write_text("bbbb  codex-package-x86_64-unknown-linux-musl.tar.gz\n")
            second = image_builder._rootfs_digest(self.dockerfile)
        self.assertNotEqual(first, second)

    def test_send_build_context_noop_without_copy(self):
        df = self.root / "None"
        df.write_text("FROM x\n")
        with self._patch_root(), \
             patch.object(image_builder.subprocess, "Popen") as popen:
            image_builder._send_build_context(Path("/k"), "10.0.0.1", df, "/tmp/c")
        popen.assert_not_called()

    def test_send_build_context_streams_copied_files_to_vm(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with self._patch_root(), \
             patch.object(image_builder.subprocess, "Popen") as popen, \
             patch.object(image_builder.subprocess, "run",
                          return_value=completed) as run:
            popen.return_value.stdout = None
            popen.return_value.returncode = 0
            popen.return_value.wait.return_value = 0
            image_builder._send_build_context(
                Path("/k"), "10.0.0.1", self.dockerfile, "/tmp/c")
        tar_argv = popen.call_args.args[0]
        self.assertEqual(tar_argv[:3], ["tar", "-C", str(self.root)])
        self.assertIn("--", tar_argv)
        self.assertIn(
            "bot_bottle/contrib/codex/codex-package_SHA256SUMS", tar_argv)
        self.assertIn("tar -C /tmp/c/ctx -xf -", run.call_args.args[0][-1])


class TestSmokeTest(unittest.TestCase):
    def test_buildah_receives_centralized_image_build_args(self):
        with patch.object(
            image_builder,
            "_ssh_streamed",
            return_value=0,
        ) as ssh, patch.object(image_builder, "info"):
            image_builder._buildah_build(
                Path("/k"),
                "10.0.0.1",
                "/tmp/context",
                "agent:test",
                {"NODE_BASE_IMAGE": "node:pinned"},
            )
        script = ssh.call_args.args[2]
        self.assertIn("--build-arg NODE_BASE_IMAGE=node:pinned", script)

    def test_empty_argv_is_noop(self):
        with patch.object(image_builder, "_ssh") as ssh:
            image_builder._smoke_test(Path("/k"), "10.0.0.1", "tag", "ctr", ())
        ssh.assert_not_called()

    def test_failed_smoke_dies(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, stdout="broken", stderr="")
        with patch.object(image_builder, "_ssh", return_value=result), \
             self.assertRaises(SystemExit):
            image_builder._smoke_test(Path("/k"), "10.0.0.1", "tag", "ctr", ("claude", "--version"))


if __name__ == "__main__":
    unittest.main()
