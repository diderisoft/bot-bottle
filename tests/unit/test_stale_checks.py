"""Unit: stale-image check functions across backends, and the
BottleBackend.launch template method (prelaunch_checks + build_or_load_images).

No real images or containers are used — all Docker/container/smolmachine
calls are mocked at the module boundary."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from bot_bottle.contract.hostctl import Settings


def _bottle_cm(bottle: Any) -> MagicMock:
    """Return a mock context manager that yields `bottle`."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=bottle)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# BottleBackend.launch template — prelaunch_checks + _build_or_load_images
# ---------------------------------------------------------------------------

class TestBottleBackendLaunchTemplate(unittest.TestCase):
    """Verify the concrete launch() method on BottleBackend calls
    _build_or_load_images and _launch_impl, and that prelaunch_checks is a no-op
    on the base class."""

    def _make_backend(self) -> Any:
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        return DockerBottleBackend(Settings())

    def test_launch_delegates_to_build_or_load_and_launch_impl(self) -> None:
        from bot_bottle.backend import BottleImages
        backend = self._make_backend()
        plan = cast(Any, SimpleNamespace())
        bottle = MagicMock()
        images = BottleImages(agent="agent:latest")
        with patch.object(
            backend, "_build_or_load_images", return_value=images,
        ) as build_mock, patch.object(
            backend, "_launch_impl",
            return_value=_bottle_cm(bottle),
        ) as impl_mock:
            with backend.launch(plan):
                pass
        build_mock.assert_called_once_with(plan)
        impl_mock.assert_called_once_with(plan, images)

    def test_noop_default_prelaunch_checks(self) -> None:
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        from bot_bottle.backend import BottleBackend
        backend = DockerBottleBackend(Settings())
        # Base-class prelaunch_checks is a no-op — must not raise.
        BottleBackend.prelaunch_checks(backend, cast(Any, SimpleNamespace()))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Docker backend stale_checks
# ---------------------------------------------------------------------------

class TestDockerStaleChecks(unittest.TestCase):
    def _plan(self, policy: str = "cached", slug: str = "dev-abc") -> Any:
        spec = SimpleNamespace(image_policy=policy)
        provision = SimpleNamespace(image="bot-bottle-agent:latest")
        return cast(Any, SimpleNamespace(
            spec=spec,
            settings=Settings(),
            slug=slug,
            image="bot-bottle-agent:latest",
            agent_provision=provision,
        ))

    def test_fresh_policy_is_noop(self) -> None:
        from bot_bottle.backend.docker import launch as mod
        with patch.object(mod, "read_committed_image") as rci, \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan("fresh"))
        rci.assert_not_called()
        cs.assert_not_called()

    def test_committed_image_present_checks_only_committed(self) -> None:
        from bot_bottle.backend.docker import launch as mod
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with patch.object(mod, "read_committed_image", return_value="committed:latest"), \
             patch.object(mod.docker_mod, "image_exists", return_value=True), \
             patch.object(mod.docker_mod, "image_created_at", return_value=ts), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        cs.assert_called_once()
        self.assertIn("committed:latest", cs.call_args.args[0])

    def test_no_committed_image_checks_agent(self) -> None:
        from bot_bottle.backend.docker import launch as mod
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        plan = self._plan()
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.docker_mod, "image_exists", return_value=True), \
             patch.object(mod.docker_mod, "image_created_at", return_value=ts), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(plan)
        cs.assert_called_once()
        self.assertIn(plan.image, cs.call_args.args[0])

    def test_image_not_present_skips_check(self) -> None:
        from bot_bottle.backend.docker import launch as mod
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.docker_mod, "image_exists", return_value=False), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        cs.assert_not_called()

    def test_only_sidecar_missing_checks_only_agent(self) -> None:
        from bot_bottle.backend.docker import launch as mod
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        plan = self._plan()

        def image_exists(ref: str) -> bool:
            return ref == plan.image  # Only agent present; sidecar missing.

        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.docker_mod, "image_exists", side_effect=image_exists), \
             patch.object(mod.docker_mod, "image_created_at", return_value=ts), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(plan)
        self.assertEqual(1, cs.call_count)
        self.assertIn(plan.image, cs.call_args.args[0])

    def test_backend_prelaunch_checks_delegates(self) -> None:
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        from bot_bottle.backend.docker import launch as mod
        backend = DockerBottleBackend(Settings())
        plan = self._plan()
        with patch.object(mod, "stale_checks") as sc:
            backend.prelaunch_checks(plan)  # type: ignore[arg-type]
        sc.assert_called_once_with(plan)


# ---------------------------------------------------------------------------
# macOS container backend stale_checks
# ---------------------------------------------------------------------------

class TestMacosContainerStaleChecks(unittest.TestCase):
    def _plan(self, policy: str = "cached", slug: str = "dev-abc") -> Any:
        return cast(Any, SimpleNamespace(
            spec=SimpleNamespace(image_policy=policy),
            settings=Settings(),
            slug=slug,
            image="bot-bottle-agent:latest",
        ))

    def test_fresh_policy_is_noop(self) -> None:
        from bot_bottle.backend.macos_container import launch as mod
        with patch.object(mod, "read_committed_image") as rci, \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan("fresh"))
        rci.assert_not_called()
        cs.assert_not_called()

    def test_committed_image_present_checks_only_committed(self) -> None:
        from bot_bottle.backend.macos_container import launch as mod
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with patch.object(mod, "read_committed_image", return_value="committed:latest"), \
             patch.object(mod.container_mod, "image_exists", return_value=True), \
             patch.object(mod.container_mod, "image_created_at", return_value=ts), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        cs.assert_called_once()
        self.assertIn("committed:latest", cs.call_args.args[0])

    def test_no_committed_image_checks_agent(self) -> None:
        from bot_bottle.backend.macos_container import launch as mod
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        plan = self._plan()
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.container_mod, "image_exists", return_value=True), \
             patch.object(mod.container_mod, "image_created_at", return_value=ts), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(plan)
        cs.assert_called_once()
        self.assertIn(plan.image, cs.call_args.args[0])

    def test_image_not_present_skips_check(self) -> None:
        from bot_bottle.backend.macos_container import launch as mod
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.container_mod, "image_exists", return_value=False), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        cs.assert_not_called()

    def test_backend_prelaunch_checks_delegates(self) -> None:
        from bot_bottle.backend.macos_container.backend import MacosContainerBottleBackend
        from bot_bottle.backend.macos_container import launch as mod
        backend = MacosContainerBottleBackend(Settings())
        plan = self._plan()
        with patch.object(mod, "stale_checks") as sc:
            backend.prelaunch_checks(plan)  # type: ignore[arg-type]
        sc.assert_called_once_with(plan)


# ---------------------------------------------------------------------------
# Firecracker cached rootfs selection and stale checks
# ---------------------------------------------------------------------------

class TestFirecrackerCachedRootfs(unittest.TestCase):
    def _plan(self, policy: str = "cached") -> Any:
        from bot_bottle.manifest import ManifestImageOverlay
        return cast(Any, SimpleNamespace(
            spec=SimpleNamespace(image_policy=policy),
            settings=Settings(),
            slug="dev-abc",
            image="bot-bottle-agent:latest",
            dockerfile_path="/repo/Dockerfile",
            agent_provider_template="claude",
            manifest=SimpleNamespace(bottle=SimpleNamespace(agent_provider=SimpleNamespace(
                overlay=ManifestImageOverlay(), builds_custom_image=False,
            ))),
        ))

    def test_cached_policy_reuses_ready_rootfs_without_building(self) -> None:
        from bot_bottle.backend.firecracker import launch as mod
        cached = Path("/cache/rootfs/agent-deadbeef")
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(
                 mod.image_builder, "cached_agent_rootfs_dir", return_value=cached,
             ), \
             patch.object(mod.image_builder, "build_agent_rootfs_dir") as build:
            self.assertEqual(cached, mod.build_or_load_agent_base(self._plan()))
        build.assert_not_called()

    def test_cached_policy_fails_when_rootfs_is_missing(self) -> None:
        from bot_bottle.backend.firecracker import launch as mod
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(
                 mod.image_builder, "cached_agent_rootfs_dir", return_value=None,
             ), \
             patch.object(mod.image_builder, "build_agent_rootfs_dir") as build, \
             self.assertRaises(SystemExit):
            mod.build_or_load_agent_base(self._plan())
        build.assert_not_called()

    def test_published_base_uses_the_ext4_artifact(self) -> None:
        from bot_bottle.backend.firecracker import launch as mod
        from bot_bottle.published_artifacts import FORMAT_EXT4, Artifact
        pin = Artifact(image="bot-bottle-claude", format=FORMAT_EXT4, platform="linux/amd64",
                       url="https://example.invalid/r.ext4.gz", digest="sha256:" + "e" * 64)
        prepared = Path("/cache/rootfs/agent-ext4-x.ext4")
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.agent_image, "resolve_agent_pin", return_value=pin) as resolve, \
             patch.object(mod.image_builder, "acquire_agent_image_ext4",
                          return_value=prepared) as acquire:
            plan = self._plan("build")
            self.assertEqual(prepared, mod.build_or_load_agent_base(plan))
        self.assertEqual(FORMAT_EXT4, resolve.call_args.kwargs["fmt"])
        acquire.assert_called_once_with(pin, plan.settings)

    def test_overlay_still_resolves_the_oci_artifact(self) -> None:
        from bot_bottle.backend.firecracker import launch as mod
        from bot_bottle.manifest import ManifestImageOverlay
        from bot_bottle.published_artifacts import FORMAT_OCI, Artifact
        pin = Artifact(image="bot-bottle-claude", format=FORMAT_OCI, platform="linux/amd64",
                       url="reg/o/bot-bottle-claude:0.1.0-amd64", digest="sha256:" + "f" * 64)
        plan = self._plan("build")
        plan.manifest.bottle.agent_provider.overlay = ManifestImageOverlay(apt=("jq",))
        plan.spec.user_cwd = "/tmp"
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.agent_image, "resolve_agent_pin", return_value=pin) as resolve, \
             patch.object(mod.image_builder, "acquire_agent_overlay_rootfs_dir",
                          return_value=Path("/cache/o")) as overlay:
            mod.build_or_load_agent_base(plan)
        self.assertEqual(FORMAT_OCI, resolve.call_args.kwargs["fmt"])
        self.assertIs(pin, overlay.call_args.args[0])

    def test_custom_dockerfile_builds_even_when_a_pin_exists(self) -> None:
        from bot_bottle.backend.firecracker import launch as mod
        plan = self._plan("build")
        plan.manifest.bottle.agent_provider.builds_custom_image = True
        built = Path("/cache/rootfs/agent-custom")
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.agent_image, "resolve_agent_pin") as pin, \
             patch.object(mod.image_builder, "build_agent_rootfs_dir", return_value=built) as build:
            self.assertEqual(built, mod.build_or_load_agent_base(plan))
        pin.assert_not_called()
        build.assert_called_once()

    def test_overlay_without_a_pin_fails_instead_of_dropping_it(self) -> None:
        from bot_bottle.agent_overlay import OverlayError
        from bot_bottle.backend.firecracker import launch as mod
        from bot_bottle.manifest import ManifestImageOverlay
        for policy in ("cached", "build"):
            plan = self._plan(policy)
            plan.manifest.bottle.agent_provider.overlay = ManifestImageOverlay(apt=("jq",))
            with self.subTest(policy=policy), \
                 patch.object(mod, "read_committed_image", return_value=""), \
                 patch.object(mod.agent_image, "resolve_agent_pin", return_value=None), \
                 patch.object(mod.image_builder, "cached_agent_rootfs_dir") as cached, \
                 patch.object(mod.image_builder, "build_agent_rootfs_dir") as build, \
                 self.assertRaises(OverlayError):
                mod.build_or_load_agent_base(plan)
            cached.assert_not_called()
            build.assert_not_called()

    def test_stale_checks_use_ready_marker_timestamp(self) -> None:
        from bot_bottle.backend.firecracker import launch as mod
        cached = Path("/cache/rootfs/agent-deadbeef")
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(
                 mod.image_builder, "cached_agent_rootfs_dir", return_value=cached,
             ), \
             patch.object(mod, "check_stale_path") as check:
            mod.stale_checks(self._plan())
        check.assert_called_once_with(f"agent rootfs {cached}", cached / ".bb-ready")

    def test_stale_checks_look_at_the_published_base_launch_would_use(self) -> None:
        import tempfile
        from bot_bottle.backend.firecracker import launch as mod
        from bot_bottle.published_artifacts import FORMAT_EXT4, Artifact
        pin = Artifact(image="bot-bottle-claude", format=FORMAT_EXT4, platform="linux/amd64",
                       url="https://example.invalid/r.ext4.gz", digest="sha256:" + "e" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp) / "agent-ext4-x.ext4"
            prepared.write_bytes(b"")
            with patch.object(mod, "read_committed_image", return_value=""), \
                 patch.object(mod.agent_image, "resolve_agent_pin", return_value=pin), \
                 patch.object(mod.image_builder, "cached_agent_image_ext4",
                              return_value=prepared), \
                 patch.object(mod, "check_stale_path") as check:
                mod.stale_checks(self._plan())
        check.assert_called_once_with(
            f"agent rootfs {prepared}", prepared.with_suffix(".ready"))

    def test_backend_prelaunch_checks_delegates(self) -> None:
        from bot_bottle.backend.firecracker.backend import FirecrackerBottleBackend
        from bot_bottle.backend.firecracker import launch as mod
        plan = self._plan()
        with patch.object(mod, "stale_checks") as checks:
            FirecrackerBottleBackend(Settings()).prelaunch_checks(plan)
        checks.assert_called_once_with(plan)


if __name__ == "__main__":
    unittest.main()
