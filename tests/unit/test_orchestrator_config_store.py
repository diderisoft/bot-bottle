"""Unit: OrchestratorConfigStore and resolve_teardown_timeout.

Also verifies the lifecycle ordering invariant: resolve_teardown_timeout()
must be called before launch_consolidated() / register_agent() so that a
resolver failure cannot leave an orphaned registration with no teardown
callback.
"""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import ModuleType

from bot_bottle.orchestrator.store.config_store import (
    DEFAULT_TEARDOWN_TIMEOUT_SECONDS,
    TEARDOWN_TIMEOUT_ENV,
    OrchestratorConfigStore,
    resolve_teardown_timeout,
)


class TestOrchestratorConfigStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        self.store = OrchestratorConfigStore(self.db)
        self.store.migrate()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_returns_none_when_not_set(self) -> None:
        self.assertIsNone(self.store.get_teardown_timeout_seconds())

    def test_set_and_get_roundtrip(self) -> None:
        self.store.set_teardown_timeout_seconds(42.5)
        self.assertEqual(42.5, self.store.get_teardown_timeout_seconds())

    def test_set_overwrites_existing_value(self) -> None:
        self.store.set_teardown_timeout_seconds(10.0)
        self.store.set_teardown_timeout_seconds(20.0)
        self.assertEqual(20.0, self.store.get_teardown_timeout_seconds())

    def test_delete_clears_value_and_returns_true(self) -> None:
        self.store.set_teardown_timeout_seconds(30.0)
        deleted = self.store.delete_teardown_timeout_seconds()
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get_teardown_timeout_seconds())

    def test_delete_absent_returns_false(self) -> None:
        self.assertFalse(self.store.delete_teardown_timeout_seconds())

    def test_is_migrated_true_after_migrate(self) -> None:
        self.assertTrue(self.store.is_migrated())

    def test_is_migrated_false_before_migrate(self) -> None:
        store = OrchestratorConfigStore(Path(self._tmp.name) / "new.db")
        self.assertFalse(store.is_migrated())


class TestResolveTeardownTimeout(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop(TEARDOWN_TIMEOUT_ENV, None)

    def test_returns_default_when_nothing_configured(self) -> None:
        self.assertEqual(DEFAULT_TEARDOWN_TIMEOUT_SECONDS, resolve_teardown_timeout())

    def test_env_var_overrides_default(self) -> None:
        os.environ[TEARDOWN_TIMEOUT_ENV] = "99"
        self.assertEqual(99.0, resolve_teardown_timeout())

    def test_invalid_env_var_falls_through_to_default(self) -> None:
        os.environ[TEARDOWN_TIMEOUT_ENV] = "not-a-number"
        self.assertEqual(DEFAULT_TEARDOWN_TIMEOUT_SECONDS, resolve_teardown_timeout())

    def test_non_positive_env_var_falls_through_to_default(self) -> None:
        os.environ[TEARDOWN_TIMEOUT_ENV] = "0"
        self.assertEqual(DEFAULT_TEARDOWN_TIMEOUT_SECONDS, resolve_teardown_timeout())

    def test_never_opens_a_database(self) -> None:
        # The stored value is the orchestrator's, in its registry.db; a
        # launch on the host never reads it.
        with unittest.mock.patch("sqlite3.connect") as connect:
            resolve_teardown_timeout()
        connect.assert_not_called()


class TestTeardownTimeoutResolvedBeforeRegistration(unittest.TestCase):
    """Ordering invariant: if resolve_teardown_timeout() raises, the bottle
    must not yet be registered — no orphaned state can result."""

    def _src(self, module: ModuleType) -> str:
        return inspect.getsource(module)

    def test_docker_resolves_timeout_before_launch_consolidated(self) -> None:
        from bot_bottle.backend.docker import launch
        src = self._src(launch)
        resolve_at = src.index("teardown_timeout = resolve_teardown_timeout()")
        launch_at = src.index("ctx = launch_consolidated(")
        self.assertLess(resolve_at, launch_at)

    def test_firecracker_resolves_timeout_before_launch_consolidated(self) -> None:
        from bot_bottle.backend.firecracker import launch
        src = self._src(launch)
        resolve_at = src.index("teardown_timeout = resolve_teardown_timeout()")
        launch_at = src.index("ctx = launch_consolidated(")
        self.assertLess(resolve_at, launch_at)

    def test_macos_resolves_timeout_before_register_agent(self) -> None:
        from bot_bottle.backend.macos_container import launch
        src = self._src(launch)
        resolve_at = src.index("teardown_timeout = resolve_teardown_timeout()")
        register_at = src.index("ctx = register_agent(")
        self.assertLess(resolve_at, register_at)


if __name__ == "__main__":
    unittest.main()
