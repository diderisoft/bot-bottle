"""Unit: stale-image check functions across backends, and the
BottleBackend.launch template method (skip_stale flag).

No real images or containers are used — all Docker/container/smolmachine
calls are mocked at the module boundary."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from bot_bottle.image_cache import StaleImageError


# ---------------------------------------------------------------------------
# BottleBackend.launch template — _image_stale_checks + skip_stale
# ---------------------------------------------------------------------------

class TestBottleBackendLaunchTemplate(unittest.TestCase):
    """Verify the concrete launch() method on BottleBackend calls
    _image_stale_checks unless skip_stale=True, then delegates to _launch_impl."""

    def _make_backend(self):
        from bot_bottle.backend import BottleBackend

        class _FakeBottle:
            name = "fake"

        @contextlib.contextmanager
        def _fake_impl(plan):
            yield _FakeBottle()

        class _FakeBackend(BottleBackend):
            name = "fake"

            @classmethod
            def is_available(cls): return True

            def _preflight(self): pass
            def _build_guest_env(self, env): return {}
            def _resolve_plan(self, spec, **kw): return cast(Any, SimpleNamespace())

            @contextlib.contextmanager
            def _launch_impl(self, plan):
                yield _FakeBottle()

            def prepare_cleanup(self): return cast(Any, None)
            def cleanup(self, plan): pass
            def enumerate_active(self): return []

        return _FakeBackend()

    def test_stale_checks_called_by_default(self):
        backend = self._make_backend()
        called = []
        backend._image_stale_checks = lambda plan: called.append(True)  # type: ignore[method-assign]
        plan = cast(Any, SimpleNamespace())
        with backend.launch(plan):
            pass
        self.assertEqual([True], called)

    def test_skip_stale_bypasses_stale_checks(self):
        backend = self._make_backend()
        backend._image_stale_checks = lambda plan: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("should not be called")
        )
        plan = cast(Any, SimpleNamespace())
        with backend.launch(plan, skip_stale=True):
            pass

    def test_noop_default_image_stale_checks(self):
        from bot_bottle.backend import BottleBackend

        class _MinimalBackend(BottleBackend):
            name = "minimal"

            @classmethod
            def is_available(cls): return True

            def _preflight(self): pass
            def _build_guest_env(self, env): return {}
            def _resolve_plan(self, spec, **kw): return cast(Any, SimpleNamespace())

            @contextlib.contextmanager
            def _launch_impl(self, plan):
                yield cast(Any, SimpleNamespace(name="x"))

            def prepare_cleanup(self): return cast(Any, None)
            def cleanup(self, plan): pass
            def enumerate_active(self): return []

        backend = _MinimalBackend()
        # The default _image_stale_checks must not raise — it's a no-op.
        backend._image_stale_checks(cast(Any, SimpleNamespace()))


# ---------------------------------------------------------------------------
# Docker backend stale_checks
# ---------------------------------------------------------------------------

class TestDockerStaleChecks(unittest.TestCase):
    from bot_bottle.backend.docker import launch as _docker_launch_mod

    def _plan(self, policy: str = "cached", slug: str = "dev-abc") -> Any:
        spec = SimpleNamespace(image_policy=policy)
        provision = SimpleNamespace(image="bot-bottle-agent:latest")
        return cast(Any, SimpleNamespace(
            spec=spec,
            slug=slug,
            image="bot-bottle-agent:latest",
            agent_provision=provision,
        ))

    def test_fresh_policy_is_noop(self):
        from bot_bottle.backend.docker import launch as mod
        with patch.object(mod, "read_committed_image") as rci, \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan("fresh"))
        rci.assert_not_called()
        cs.assert_not_called()

    def test_committed_image_present_checks_only_committed(self):
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

    def test_no_committed_image_checks_agent_and_sidecar(self):
        from bot_bottle.backend.docker import launch as mod
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.docker_mod, "image_exists", return_value=True), \
             patch.object(mod.docker_mod, "image_created_at", return_value=ts), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        # Should have been called for both agent and sidecar images.
        self.assertEqual(2, cs.call_count)

    def test_image_not_present_skips_check(self):
        from bot_bottle.backend.docker import launch as mod
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.docker_mod, "image_exists", return_value=False), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        cs.assert_not_called()

    def test_backend_image_stale_checks_delegates(self):
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        from bot_bottle.backend.docker import launch as mod
        backend = DockerBottleBackend()
        plan = self._plan()
        with patch.object(mod, "stale_checks") as sc:
            backend._image_stale_checks(plan)
        sc.assert_called_once_with(plan)

    def test_no_committed_image_only_sidecar_missing_skips_check(self):
        # When no committed image, stale_checks checks each image that EXISTS.
        # If an image isn't present, check_stale is not called for it.
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
        # Only agent was checked (sidecar not present → skipped).
        self.assertEqual(1, cs.call_count)
        self.assertIn(plan.image, cs.call_args.args[0])


# ---------------------------------------------------------------------------
# smolmachines backend stale_checks
# ---------------------------------------------------------------------------

class TestSmolmachinesStaleChecks(unittest.TestCase):
    def _plan(self, policy: str = "cached") -> Any:
        return cast(Any, SimpleNamespace(
            spec=SimpleNamespace(image_policy=policy),
            slug="dev-abc",
            agent_image="bot-bottle-claude:latest",
        ))

    def test_fresh_policy_is_noop(self):
        from bot_bottle.backend.smolmachines import launch as mod
        with patch.object(mod, "read_committed_image") as rci, \
             patch.object(mod, "check_stale_path") as csp:
            mod.stale_checks(self._plan("fresh"))
        rci.assert_not_called()
        csp.assert_not_called()

    def test_committed_artifact_found_checks_it(self):
        from bot_bottle.backend.smolmachines import launch as mod
        with tempfile.NamedTemporaryFile(suffix=".smolmachine") as f:
            committed_path = f.name
            with patch.object(mod, "read_committed_image", return_value=committed_path), \
                 patch.object(mod.docker_mod, "image_exists", return_value=False), \
                 patch.object(mod, "check_stale_path") as csp:
                mod.stale_checks(self._plan())
        csp.assert_called_once()
        self.assertIn("agent smolmachine", csp.call_args.args[0])

    def test_no_committed_agent_artifact_in_cache_is_checked(self):
        from bot_bottle.backend.smolmachines import launch as mod
        digest = "abcdef0123456789"
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / f"{digest}.smolmachine.smolmachine"
            artifact.write_text("")
            with patch.object(mod, "_SMOLMACHINE_CACHE_DIR", Path(tmp)), \
                 patch.object(mod, "read_committed_image", return_value=""), \
                 patch.object(mod.docker_mod, "image_exists", return_value=True), \
                 patch.object(mod.docker_mod, "image_id",
                              return_value=f"sha256:{digest}ffffffffffffffff"), \
                 patch.object(mod, "check_stale_path") as csp:
                mod.stale_checks(self._plan())
        # At least the agent artifact check ran.
        self.assertGreaterEqual(csp.call_count, 1)

    def test_sidecar_artifact_in_cache_is_checked(self):
        from bot_bottle.backend.smolmachines import launch as mod
        digest = "1234567890abcdef"
        sidecar_digest = "fedcba9876543210"
        with tempfile.TemporaryDirectory() as tmp:
            sidecar_artifact = Path(tmp) / f"{sidecar_digest}.smolmachine.smolmachine"
            sidecar_artifact.write_text("")

            def image_exists(ref: str) -> bool:
                return True

            def image_id(ref: str) -> str:
                if "sidecar" in ref or ref == mod._bundle.SIDECAR_BUNDLE_IMAGE:
                    return f"sha256:{sidecar_digest}ffffffff"
                return f"sha256:{digest}ffffffff"

            with patch.object(mod, "_SMOLMACHINE_CACHE_DIR", Path(tmp)), \
                 patch.object(mod, "read_committed_image", return_value=""), \
                 patch.object(mod.docker_mod, "image_exists", side_effect=image_exists), \
                 patch.object(mod.docker_mod, "image_id", side_effect=image_id), \
                 patch.object(mod, "check_stale_path") as csp:
                mod.stale_checks(self._plan())
        # sidecar artifact is present, so check_stale_path should fire for it.
        sidecar_calls = [c for c in csp.call_args_list if "sidecar" in c.args[0]]
        self.assertTrue(len(sidecar_calls) >= 1)

    def test_backend_image_stale_checks_delegates(self):
        from bot_bottle.backend.smolmachines.backend import SmolmachinesBottleBackend
        from bot_bottle.backend.smolmachines import launch as mod
        backend = SmolmachinesBottleBackend()
        plan = self._plan()
        with patch.object(mod, "stale_checks") as sc:
            backend._image_stale_checks(plan)
        sc.assert_called_once_with(plan)


# ---------------------------------------------------------------------------
# macOS container backend stale_checks
# ---------------------------------------------------------------------------

class TestMacosContainerStaleChecks(unittest.TestCase):
    def _plan(self, policy: str = "cached", slug: str = "dev-abc") -> Any:
        return cast(Any, SimpleNamespace(
            spec=SimpleNamespace(image_policy=policy),
            slug=slug,
            image="bot-bottle-agent:latest",
        ))

    def test_fresh_policy_is_noop(self):
        from bot_bottle.backend.macos_container import launch as mod
        with patch.object(mod, "read_committed_image") as rci, \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan("fresh"))
        rci.assert_not_called()
        cs.assert_not_called()

    def test_committed_image_present_checks_only_committed(self):
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

    def test_no_committed_image_checks_agent_and_sidecar(self):
        from bot_bottle.backend.macos_container import launch as mod
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.container_mod, "image_exists", return_value=True), \
             patch.object(mod.container_mod, "image_created_at", return_value=ts), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        self.assertEqual(2, cs.call_count)

    def test_image_not_present_skips_check(self):
        from bot_bottle.backend.macos_container import launch as mod
        with patch.object(mod, "read_committed_image", return_value=""), \
             patch.object(mod.container_mod, "image_exists", return_value=False), \
             patch.object(mod, "check_stale") as cs:
            mod.stale_checks(self._plan())
        cs.assert_not_called()

    def test_backend_image_stale_checks_delegates(self):
        from bot_bottle.backend.macos_container.backend import MacosContainerBottleBackend
        from bot_bottle.backend.macos_container import launch as mod
        backend = MacosContainerBottleBackend()
        plan = self._plan()
        with patch.object(mod, "stale_checks") as sc:
            backend._image_stale_checks(plan)
        sc.assert_called_once_with(plan)


if __name__ == "__main__":
    unittest.main()
