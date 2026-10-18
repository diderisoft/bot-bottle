"""Unit tests for the docker-free Firecracker agent-image builder.

The VM boot / SSH / buildah plumbing (`_build_in_infra`) is integration-tested
on a KVM host; here we cover the cache decision, the boot-bit injection, and
the smoke-test no-op — the logic that must hold without a VM.
"""

from __future__ import annotations

import shutil
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

    def _ext4_artifact(self, digest_char: str = "a"):
        from bot_bottle.published_artifacts import Artifact
        return Artifact(
            image="bot-bottle-claude", format="ext4", platform="linux/amd64",
            url="https://example.invalid/rootfs.amd64.ext4.gz",
            digest="sha256:" + digest_char * 64, version="0.1.0",
        )

    def _dropbear(self) -> Path:
        dropbear = self.cache / "dropbear"
        dropbear.write_bytes(b"\x7fELF-fake-dropbear")
        return dropbear

    def test_pulled_ext4_cache_hit_skips_download(self):
        artifact = self._ext4_artifact()
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder.util, "dropbear_path", return_value=self._dropbear()), \
             patch.object(image_builder.image_registry, "ensure_ext4") as ensure:
            image = image_builder._image_ext4_path(artifact)
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"prepared")
            image.with_suffix(".ready").write_text("ok\n")
            out = image_builder.acquire_agent_image_ext4(artifact)
        ensure.assert_not_called()
        self.assertEqual(image, out)

    def test_pulled_ext4_rejects_a_non_ext4_artifact(self):
        from dataclasses import replace
        artifact = replace(self._ext4_artifact(), format="oci")
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             self.assertRaises(SystemExit):
            image_builder.acquire_agent_image_ext4(artifact)

    def test_ext4_key_changes_with_guest_init_and_dropbear(self):
        artifact = self._ext4_artifact()
        dropbear = self._dropbear()
        with patch.object(image_builder.util, "dropbear_path", return_value=dropbear):
            base = image_builder._image_ext4_key(artifact)
            with patch.object(image_builder.util, "_GUEST_INIT", "#!/bin/sh\necho changed\n"):
                self.assertNotEqual(base, image_builder._image_ext4_key(artifact))
            dropbear.write_bytes(b"different dropbear")
            self.assertNotEqual(base, image_builder._image_ext4_key(artifact))

    @unittest.skipUnless(
        all(shutil.which(t) for t in ("mke2fs", "debugfs", "e2fsck", "resize2fs")),
        "e2fsprogs not installed",
    )
    def test_pulled_ext4_is_prepared_with_guest_boot_and_slack(self):
        import gzip as gz_mod
        src = self.cache / "src"
        (src / "etc").mkdir(parents=True)
        (src / "etc" / "hello").write_text("published\n")
        raw = self.cache / "published.ext4"
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", str(src), "-F", str(raw), "16M"],
                       check=True)
        gz = self.cache / "published.ext4.gz"
        gz.write_bytes(gz_mod.compress(raw.read_bytes()))
        artifact = self._ext4_artifact("c")
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder.util, "dropbear_path", return_value=self._dropbear()), \
             patch.object(image_builder, "_PULLED_IMAGE_SLACK_MIB", 8), \
             patch.object(image_builder.image_registry, "ensure_ext4", return_value=gz):
            out = image_builder.acquire_agent_image_ext4(artifact)
        self.assertTrue(out.with_suffix(".ready").is_file())
        self.assertEqual(24 << 20, out.stat().st_size)  # 16M + 8M slack
        for name in ("bb-init", "bb-dropbear"):
            stat = subprocess.run(["debugfs", "-R", f"stat /{name}", str(out)],
                                  capture_output=True, text=True, check=True).stdout
            self.assertIn("Mode:  0755", stat)
        init = subprocess.run(["debugfs", "-R", "cat /bb-init", str(out)],
                              capture_output=True, text=True, check=True).stdout
        self.assertEqual(image_builder.util._GUEST_INIT, init)
        hello = subprocess.run(["debugfs", "-R", "cat /etc/hello", str(out)],
                               capture_output=True, text=True, check=True).stdout
        self.assertEqual("published\n", hello)



    def test_overlay_cache_hit_skips_build(self):
        from bot_bottle.manifest import ManifestImageOverlay
        base_ref = "reg/o/bot-bottle-claude@sha256:" + "d" * 64
        overlay = ManifestImageOverlay(apt=("jq",))
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache):
            digest = image_builder._overlay_rootfs_digest(base_ref, overlay, self.cache)
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


class TestMakeBottleRootfs(unittest.TestCase):
    def test_image_base_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.ext4"
            base.write_bytes(b"ext4-bytes")
            out = Path(tmp) / "run" / "rootfs.ext4"
            image_builder.util.make_bottle_rootfs(base, out)
            self.assertEqual(b"ext4-bytes", out.read_bytes())
            self.assertEqual(b"ext4-bytes", base.read_bytes())

    def test_directory_base_goes_through_mke2fs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            base.mkdir()
            out = Path(tmp) / "rootfs.ext4"
            with patch.object(image_builder.util, "build_rootfs_ext4") as build:
                image_builder.util.make_bottle_rootfs(base, out)
            build.assert_called_once_with(base, out)


if __name__ == "__main__":
    unittest.main()
