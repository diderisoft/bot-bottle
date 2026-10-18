"""Unit tests for the docker-free Firecracker agent-image builder.

The VM boot / SSH / buildah plumbing (`_infra_build`) is integration-tested
on a KVM host; here we cover the cache decision, the boot-bit injection, and
the smoke-test no-op — the logic that must hold without a VM.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bot_bottle.backend.firecracker import image_builder


def _patch_dropbear(case: unittest.TestCase, where: Path) -> None:
    """Every agent cache key hashes the static dropbear; give tests a fake."""
    dropbear = where / "dropbear"
    dropbear.write_bytes(b"\x7fELF-fake-dropbear")
    patcher = patch.object(image_builder.util, "dropbear_path", return_value=dropbear)
    patcher.start()
    case.addCleanup(patcher.stop)


class TestBuildAgentRootfsDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self.dockerfile = self.cache / "Dockerfile"
        self.dockerfile.write_text("FROM node:22-slim\n")
        self.addCleanup(self._tmp.cleanup)
        _patch_dropbear(self, self.cache)

    def test_cache_hit_skips_rebuild(self):
        digest = image_builder._rootfs_digest(self.dockerfile)
        base = self.cache / "rootfs" / f"agent-{digest}"
        base.mkdir(parents=True)
        (base / ".bb-ready").write_text("ok\n")
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_infra_build") as build:
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
             patch.object(image_builder, "_infra_build") as build, \
             patch.object(image_builder.util, "inject_guest_boot") as inject:
            out = image_builder.build_agent_rootfs_dir(
                self.dockerfile, image_tag="t:latest", smoke_test=("claude", "--version"))
        build.assert_called_once()
        # smoke_test threads through to the VM build.
        self.assertEqual(("claude", "--version"), build.call_args.kwargs["smoke_test"])
        inject.assert_called_once()
        self.assertTrue((out / ".bb-ready").is_file())

    def test_failed_build_leaves_nothing_behind(self):
        def boom(**kw: Any) -> None:
            (kw["out"] / "partial").write_text("x")
            raise SystemExit(1)
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder, "_infra_build", side_effect=boom), \
             self.assertRaises(SystemExit):
            image_builder.build_agent_rootfs_dir(self.dockerfile, image_tag="t:latest")
        self.assertEqual([".build.lock"], sorted(p.name for p in (self.cache / "rootfs").iterdir()))

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
        with patch.object(image_builder.util, "dropbear_path", return_value=dropbear), \
             patch.object(image_builder.util, "cache_dir", return_value=self.cache):
            base = image_builder._image_ext4_path(artifact)
            with patch.object(image_builder.util, "_GUEST_INIT", "#!/bin/sh\necho changed\n"):
                self.assertNotEqual(base, image_builder._image_ext4_path(artifact))
            dropbear.write_bytes(b"different dropbear")
            self.assertNotEqual(base, image_builder._image_ext4_path(artifact))

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



    def _oci_pin(self, digest_char: str = "d"):
        from bot_bottle.published_artifacts import Artifact
        return Artifact(
            image="bot-bottle-claude", format="oci", platform="linux/amd64",
            url="reg/o/bot-bottle-claude:0.1.0-amd64", digest="sha256:" + digest_char * 64,
        )

    def test_overlay_cache_hit_skips_build_and_the_registry(self):
        from bot_bottle.manifest import ManifestImageOverlay
        pin = self._oci_pin()
        overlay = ManifestImageOverlay(apt=("jq",))
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache):
            with image_builder.agent_overlay.staged_overlay(
                image_builder.image_registry.pinned_reference(pin), overlay,
                template="claude", source_root=self.cache,
            ) as staged:
                base = self.cache / "rootfs" / image_builder._overlay_rootfs_name(staged)
            base.mkdir(parents=True)
            (base / ".bb-ready").write_text("ok\n")
            self.assertEqual(base, image_builder.cached_agent_overlay_rootfs_dir(
                pin, overlay, template="claude", source_root=self.cache))
            with patch.object(image_builder, "_infra_build") as build, \
                 patch.object(image_builder.image_registry, "verify_tag") as verify:
                out = image_builder.acquire_agent_overlay_rootfs_dir(
                    pin, overlay, template="claude", source_root=self.cache)
        build.assert_not_called()
        verify.assert_not_called()  # a cached overlay launches offline
        self.assertEqual(base, out)

    def _overlay_miss(self, engine: str | None) -> tuple[Any, Any]:
        from bot_bottle.manifest import ManifestImageOverlay
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder.image_registry, "verify_tag") as verify, \
             patch.object(image_builder.container_engine, "host_container_engine",
                          return_value=engine), \
             patch.object(image_builder, "_build_overlay_on_host") as host, \
             patch.object(image_builder, "_infra_build") as vm, \
             patch.object(image_builder.util, "inject_guest_boot"):
            out = image_builder.acquire_agent_overlay_rootfs_dir(
                self._oci_pin("9"), ManifestImageOverlay(apt=("jq",)), template="claude",
                source_root=self.cache)
        verify.assert_called_once()
        self.assertTrue((out / ".bb-ready").is_file())
        return host, vm

    def test_overlay_builds_on_the_host_when_an_engine_is_present(self):
        host, vm = self._overlay_miss("podman")
        host.assert_called_once()
        self.assertEqual("podman", host.call_args.args[0])
        vm.assert_not_called()

    def test_overlay_falls_back_to_the_infra_vm_without_an_engine(self):
        host, vm = self._overlay_miss(None)
        vm.assert_called_once()
        self.assertEqual("reg", vm.call_args.kwargs["registry"])
        self.assertIn(".bb-overlay.Dockerfile", str(vm.call_args.kwargs["dockerfile"]))
        self.assertNotIn(".bb-overlay.Dockerfile", vm.call_args.kwargs["ctx_rels"])
        host.assert_not_called()

    def test_forced_engine_that_is_unusable_dies(self):
        from bot_bottle.backend.container_engine import EngineUnavailable
        from bot_bottle.manifest import ManifestImageOverlay
        with patch.object(image_builder.util, "cache_dir", return_value=self.cache), \
             patch.object(image_builder.image_registry, "verify_tag"), \
             patch.object(image_builder.container_engine, "host_container_engine",
                          side_effect=EngineUnavailable("docker is down")), \
             patch.object(image_builder, "_infra_build") as vm, \
             self.assertRaises(SystemExit):
            image_builder.acquire_agent_overlay_rootfs_dir(
                self._oci_pin("8"), ManifestImageOverlay(apt=("jq",)), template="claude",
                source_root=self.cache)
        vm.assert_not_called()

    def _host_build(
        self, engine: str, *, token: str = "", build_rc: int = 0, smoke_rc: int = 0,
    ) -> tuple[list[tuple[list[str], dict[str, Any]]], Any]:
        from bot_bottle.agent_overlay import StagedOverlay
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def run(argv: list[str], **kw: Any) -> "subprocess.CompletedProcess[str]":
            calls.append((list(argv), kw))
            rc, out = 0, ""
            if "build" in argv:
                rc = build_rc
            if "run" in argv and "--rm" in argv:
                rc = smoke_rc
            if argv[:3] == ["docker", "context", "inspect"]:
                out = "unix:///run/user/1000/docker.sock\n"
            return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

        staged = StagedOverlay(
            context=Path("/ctx"), dockerfile=Path("/ctx/.bb-overlay.Dockerfile"),
            cache_key="abc123")
        env = {image_builder._ARTIFACT_TOKEN_ENV: token}  # "" = no token
        with patch.dict("os.environ", env), \
             patch.object(image_builder.subprocess, "run", side_effect=run), \
             patch.object(image_builder.util, "export_image_rootfs") as export:
            image_builder._build_overlay_on_host(
                engine, "reg.example/o/bot-bottle-claude@sha256:" + "1" * 64,
                staged, "bot-bottle-agent-overlay-abc123", self.cache / "out",
                ("claude", "--version"))
        return calls, export

    def test_host_build_pulls_with_throwaway_docker_credentials(self):
        calls, export = self._host_build("docker", token="t0k")
        login = next(c for c in calls if "login" in c[0])
        self.assertEqual("--config", login[0][1])        # throwaway config dir
        self.assertEqual("t0k", login[1]["input"])        # token via stdin, not argv
        self.assertNotIn("t0k", login[0])
        pull = next(c for c in calls if "pull" in c[0])
        self.assertEqual("--config", pull[0][1])
        # ...pointed at the user's current context, the daemon the build uses.
        self.assertIn("unix:///run/user/1000/docker.sock", pull[0])
        build = next(c for c in calls if "build" in c[0])
        self.assertNotIn("--config", build[0])           # user's own setup
        export.assert_called_once()
        self.assertEqual(["docker", "rmi", "-f", "bot-bottle-agent-overlay-abc123"], calls[-1][0])

    def test_host_build_pulls_with_a_podman_authfile(self):
        calls, _ = self._host_build("podman", token="t0k")
        self.assertIn("--authfile", next(c for c in calls if "pull" in c[0])[0])
        self.assertNotIn("--authfile", next(c for c in calls if "build" in c[0])[0])

    def test_host_build_without_a_token_uses_the_existing_login(self):
        calls, _ = self._host_build("docker")
        self.assertFalse(any("login" in c[0] or "pull" in c[0] for c in calls))
        build = next(c for c in calls if "build" in c[0])
        self.assertNotIn("--config", build[0])

    def test_host_build_failure_dies_and_still_cleans_up(self):
        with self.assertRaises(SystemExit):
            self._host_build("docker", build_rc=1)

    def test_host_build_smoke_failure_dies(self):
        with self.assertRaises(SystemExit):
            self._host_build("docker", smoke_rc=1)

    def test_infra_build_logs_in_with_the_token_on_stdin(self):
        from unittest.mock import MagicMock
        out = self.cache / "overlay-out"
        out.mkdir()
        svc = MagicMock()
        svc.orchestrator.return_value.ssh_target.return_value = (Path("/k"), "10.0.0.2")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(image_builder, "FirecrackerInfraService", return_value=svc), \
             patch.object(image_builder, "_ssh", return_value=ok) as ssh, \
             patch.object(image_builder, "_send_dockerfile") as send_df, \
             patch.object(image_builder, "_tar_into_vm") as send_ctx, \
             patch.object(image_builder, "_buildah_build") as build, \
             patch.object(image_builder, "_smoke_test"), \
             patch.object(image_builder, "_stream_rootfs") as stream, \
             patch.dict("os.environ", {image_builder._ARTIFACT_TOKEN_ENV: "TOK"}):
            image_builder._infra_build(
                dockerfile=self.dockerfile, ctx_root=self.cache, ctx_rels=["f"],
                tag="bot-bottle-agent-overlay-x", build_args={},
                smoke_test=("claude", "--version"), out=out, registry="reg")
        send_df.assert_called_once()
        self.assertEqual(["f"], send_ctx.call_args.args[3])
        build.assert_called_once()
        stream.assert_called_once()
        login = next(c for c in ssh.call_args_list if "buildah login" in c.args[2])
        self.assertEqual("TOK", login.kwargs["input"])
        self.assertTrue(all("TOK" not in c.args[2] for c in ssh.call_args_list))

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
        _patch_dropbear(self, self.root)
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

    def test_tar_into_vm_noop_without_paths(self):
        with patch.object(image_builder.subprocess, "Popen") as popen:
            image_builder._tar_into_vm(Path("/k"), "10.0.0.1", self.root, [], "/tmp/c/ctx")
        popen.assert_not_called()

    def test_tar_into_vm_streams_the_paths(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        rel = "bot_bottle/contrib/codex/codex-package_SHA256SUMS"
        with patch.object(image_builder.subprocess, "Popen") as popen, \
             patch.object(image_builder.subprocess, "run",
                          return_value=completed) as run:
            popen.return_value.stdout = None
            popen.return_value.returncode = 0
            popen.return_value.wait.return_value = 0
            image_builder._tar_into_vm(Path("/k"), "10.0.0.1", self.root, [rel], "/tmp/c/ctx")
        tar_argv = popen.call_args.args[0]
        self.assertEqual(tar_argv[:3], ["tar", "-C", str(self.root)])
        self.assertEqual(["--", rel], tar_argv[-2:])
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

    def test_failed_build_says_how_to_reset_the_build_store(self):
        with patch.object(image_builder, "_ssh_streamed", return_value=125), \
             patch.object(image_builder, "info"), \
             patch.object(image_builder, "die", side_effect=SystemExit) as die, \
             self.assertRaises(SystemExit):
            image_builder._buildah_build(Path("/k"), "10.0.0.1", "/tmp/c", "t", {})
        message = die.call_args.args[0]
        self.assertIn("build-store.ext4", message)
        self.assertIn("vm.pid", message)

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


class TestStreamRootfs(unittest.TestCase):
    def test_read_only_mount_root_leaves_base_writable(self) -> None:
        # buildah's overlay mount root is 0555 and `tar -C $mnt -cf - .`
        # carries that in the `.` entry; the guest boot bits are written into
        # `base` afterwards, so it must keep its own mode.
        with tempfile.TemporaryDirectory() as tmp:
            src, base = Path(tmp) / "mnt", Path(tmp) / "base"
            (src / "etc").mkdir(parents=True)
            (src / "etc" / "hostname").write_text("bottle\n")
            base.mkdir()
            src.chmod(0o555)
            try:
                fake_ssh = ["sh", "-c", f'tar -C "{src}" -cf - .']
                with patch.object(image_builder.util, "ssh_base_argv",
                                  return_value=fake_ssh):
                    image_builder._stream_rootfs(
                        Path("/key"), "10.0.0.1", "tag", "ctr", base)
                self.assertEqual((base / "etc" / "hostname").read_text(), "bottle\n")
                (base / "bb-dropbear").write_bytes(b"")
            finally:
                src.chmod(0o755)


class TestPruneLowStore(unittest.TestCase):
    """The pre-build prune snippet, run under sh with stub `df`/`buildah`."""

    def _run(self, free_mib: int) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir, marker = Path(tmp) / "bin", Path(tmp) / "pruned"
            bin_dir.mkdir()
            stubs = {
                "df": "echo 'Filesystem 1M-blocks Used Available Capacity Mounted'\n"
                      f"echo '/dev/vdc 16000 100 {free_mib} 1% /store'",
                "buildah": f"touch {marker}",
            }
            for name, body in stubs.items():
                stub = bin_dir / name
                stub.write_text(f"#!/bin/sh\n{body}\n")
                stub.chmod(0o755)
            subprocess.run(["sh", "-c", image_builder._PRUNE_LOW_STORE],
                           env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, check=True)
            return marker.exists()

    def test_keeps_images_with_room(self) -> None:
        self.assertFalse(self._run(image_builder._STORE_MIN_FREE_MIB))

    def test_drops_images_when_low(self) -> None:
        self.assertTrue(self._run(image_builder._STORE_MIN_FREE_MIB - 1))

    def test_build_prunes_before_buildah_build(self) -> None:
        with patch.object(image_builder, "_ssh_streamed", return_value=0) as ssh, \
                patch.object(image_builder, "info"):
            image_builder._buildah_build(Path("/k"), "10.0.0.1", "/tmp/c", "t", {})
        script = ssh.call_args.args[2]
        self.assertLess(script.index(image_builder._PRUNE_LOW_STORE),
                        script.index("buildah build"))



if __name__ == "__main__":
    unittest.main()
