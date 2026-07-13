"""Unit tests for the Orchestrator launch lifecycle (PRD 0070)."""

from __future__ import annotations

import secrets
import tempfile
import unittest
from pathlib import Path

from bot_bottle.orchestrator.broker import LaunchBroker, LaunchRequest, StubBroker
from bot_bottle.orchestrator.registry import RegistryStore
from bot_bottle.orchestrator.service import Orchestrator


class _FailingBroker(LaunchBroker):
    """Verifies the token like any broker, then fails the launch — to
    exercise the orchestrator's registry rollback."""

    def _launch(self, req: LaunchRequest) -> None:
        raise RuntimeError("launch failed")

    def _teardown(self, req: LaunchRequest) -> None:
        pass


class TestOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.secret = secrets.token_bytes(16)
        self.store = RegistryStore(Path(self._tmp.name) / "r.db")
        self.store.migrate()
        self.broker = StubBroker(self.secret)
        self.orch = Orchestrator(self.store, self.broker, self.secret)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_launch_registers_and_brokers_signed_request(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.1", image_ref="sha256:abc", slot=2)
        self.assertIsNotNone(self.store.get(rec.bottle_id))
        self.assertEqual(1, len(self.broker.launched))
        req = self.broker.launched[0]
        self.assertEqual("launch", req.op)
        self.assertEqual(rec.bottle_id, req.bottle_id)
        self.assertEqual("10.243.0.1", req.source_ip)
        self.assertEqual("sha256:abc", req.image_ref)
        self.assertEqual(2, req.slot)

    def test_launch_then_attribute(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.3")
        got = self.orch.attribute("10.243.0.3", rec.identity_token)
        assert got is not None
        self.assertEqual(rec.bottle_id, got.bottle_id)
        self.assertIsNone(self.orch.attribute("10.243.0.3", "wrong-token"))

    def test_teardown_brokers_and_deregisters(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.1")
        self.assertTrue(self.orch.teardown_bottle(rec.bottle_id))
        self.assertIsNone(self.store.get(rec.bottle_id))
        self.assertEqual(["teardown"], [r.op for r in self.broker.torn_down])

    def test_teardown_unknown_is_false(self) -> None:
        self.assertFalse(self.orch.teardown_bottle("ghost"))

    def test_launch_rolls_back_registry_on_broker_failure(self) -> None:
        orch = Orchestrator(self.store, _FailingBroker(self.secret), self.secret)
        with self.assertRaises(RuntimeError):
            orch.launch_bottle("10.243.0.9")
        self.assertEqual([], self.store.all())  # no orphan


if __name__ == "__main__":
    unittest.main()
