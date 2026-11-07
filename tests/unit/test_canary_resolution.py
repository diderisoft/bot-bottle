"""Unit: canary resolution flow in OrchestratorCore (issue #997).

Tests that canary_fire, canary_list_unresolved and canary_resolve work
end-to-end through the core, and that deregister/reconcile archive canary
events.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.unit import use_bottle_root
from bot_bottle.paths import host_db_path
from bot_bottle.contract.supervise import (
    CANARY_ACTION_ACKNOWLEDGE,
    CANARY_ACTION_TERMINATE,
    CANARY_STATUS_ACKNOWLEDGED,
    CANARY_STATUS_TERMINATED,
)
from bot_bottle.orchestrator.supervisor import Supervisor
from bot_bottle.orchestrator.service import OrchestratorCore
from bot_bottle.orchestrator.store.registry_store import RegistryStore
from bot_bottle.orchestrator.store.canary_store import CanaryStore
from bot_bottle.orchestrator.store.queue_store import QueueStore


def _migrate_all() -> None:
    db = host_db_path()
    RegistryStore(db).migrate()
    QueueStore("", db).migrate()
    CanaryStore(db).migrate()


def _core() -> OrchestratorCore:
    db = host_db_path()
    return OrchestratorCore(RegistryStore(db), Supervisor(db))


class TestCanaryFire(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-canary-svc-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")
        _migrate_all()

    def tearDown(self):
        self._restore()
        self._tmp.cleanup()

    def test_fire_returns_event_id(self):
        core = _core()
        event_id = core.canary_fire(
            "dev", reason="canary token detected", request_path="api.example.com/v1",
        )
        self.assertTrue(event_id)

    def test_fire_creates_unresolved_event(self):
        core = _core()
        event_id = core.canary_fire(
            "dev", reason="canary token detected", request_path="api.example.com/v1",
        )
        events = core.canary_list_unresolved()
        self.assertEqual(1, len(events))
        self.assertEqual(event_id, events[0]["id"])
        self.assertEqual("dev", events[0]["bottle_slug"])

    def test_list_includes_bottle_label(self):
        core = _core()
        core.canary_fire("dev", reason="r", request_path="p")
        events = core.canary_list_unresolved()
        self.assertIn("bottle_label", events[0])


class TestCanaryResolve(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-canary-resolve-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")
        _migrate_all()

    def tearDown(self):
        self._restore()
        self._tmp.cleanup()

    def _fire(self, slug: str = "dev") -> str:
        return _core().canary_fire(
            slug, reason="canary token", request_path="host/path",
        )

    def test_acknowledge_clears_event(self):
        event_id = self._fire()
        ok, err = _core().canary_resolve(event_id, action=CANARY_ACTION_ACKNOWLEDGE)
        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertEqual([], _core().canary_list_unresolved())

    def test_terminate_clears_event(self):
        event_id = self._fire()
        ok, err = _core().canary_resolve(event_id, action=CANARY_ACTION_TERMINATE,
                                         notes="suspicious")
        self.assertTrue(ok)
        self.assertEqual("", err)

    def test_unknown_action_returns_error(self):
        event_id = self._fire()
        ok, err = _core().canary_resolve(event_id, action="wave")
        self.assertFalse(ok)
        self.assertIn("action must be one of", err)

    def test_missing_event_returns_error(self):
        ok, err = _core().canary_resolve("no-such-id", action=CANARY_ACTION_ACKNOWLEDGE)
        self.assertFalse(ok)
        self.assertIn("no-such-id", err)

    def test_already_resolved_returns_error(self):
        event_id = self._fire()
        core = _core()
        core.canary_resolve(event_id, action=CANARY_ACTION_ACKNOWLEDGE)
        ok, err = core.canary_resolve(event_id, action=CANARY_ACTION_ACKNOWLEDGE)
        self.assertFalse(ok)

    def test_terminate_deregisters_bottle(self):
        """terminate deregisters the bottle from the orchestrator registry."""
        db = host_db_path()
        reg = RegistryStore(db)
        rec = reg.register("10.0.0.1")
        slug = rec.bottle_id
        core = OrchestratorCore(reg, Supervisor(db))
        event_id = core.canary_fire(slug, reason="r", request_path="p")
        ok, _ = core.canary_resolve(event_id, action=CANARY_ACTION_TERMINATE)
        self.assertTrue(ok)
        # bottle no longer in registry
        self.assertIsNone(reg.get(slug))


class TestDeregisterArchivesCanaryEvents(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-canary-dereg-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")
        _migrate_all()

    def tearDown(self):
        self._restore()
        self._tmp.cleanup()

    def test_deregister_archives_canary_events(self):
        db = host_db_path()
        reg = RegistryStore(db)
        rec = reg.register("10.0.0.2")  # already migrated in setUp
        slug = rec.bottle_id
        core = OrchestratorCore(reg, Supervisor(db))
        core.canary_fire(slug, reason="r", request_path="p")
        self.assertEqual(1, len(core.canary_list_unresolved()))
        core.deregister_bottle(slug)
        self.assertEqual([], core.canary_list_unresolved())


if __name__ == "__main__":
    unittest.main()
