"""Unit tests for the orchestrator bottle registry + attribution (PRD 0070)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.orchestrator.registry import (
    BottleRecord,
    RegistryStore,
    new_identity_token,
)


class TestRegistryStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "registry.db"
        self.store = RegistryStore(self.db)
        self.store.migrate()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_register_mints_id_and_token(self) -> None:
        rec = self.store.register("10.243.0.1")
        self.assertTrue(rec.bottle_id)
        self.assertTrue(rec.identity_token)
        self.assertEqual("active", rec.state)
        self.assertEqual(rec, self.store.get(rec.bottle_id))

    def test_identity_tokens_are_unique(self) -> None:
        tokens = {new_identity_token() for _ in range(200)}
        self.assertEqual(200, len(tokens))
        a = self.store.register("10.243.0.1")
        b = self.store.register("10.243.0.3")
        self.assertNotEqual(a.identity_token, b.identity_token)

    def test_all_and_deregister(self) -> None:
        a = self.store.register("10.243.0.1")
        b = self.store.register("10.243.0.3")
        self.assertEqual({a.bottle_id, b.bottle_id}, {r.bottle_id for r in self.store.all()})
        self.assertTrue(self.store.deregister(a.bottle_id))
        self.assertEqual([b.bottle_id], [r.bottle_id for r in self.store.all()])

    def test_deregister_missing_is_false(self) -> None:
        self.assertFalse(self.store.deregister("nope"))

    def test_explicit_id_replaces(self) -> None:
        self.store.register("10.243.0.1", bottle_id="fixed", metadata="a")
        self.store.register("10.243.0.9", bottle_id="fixed", metadata="b")
        rec = self.store.get("fixed")
        assert rec is not None
        self.assertEqual("10.243.0.9", rec.source_ip)
        self.assertEqual("b", rec.metadata)
        self.assertEqual(1, len(self.store.all()))

    def test_attribute_success_requires_ip_and_token(self) -> None:
        rec = self.store.register("10.243.0.1")
        got = self.store.attribute("10.243.0.1", rec.identity_token)
        assert got is not None
        self.assertEqual(rec.bottle_id, got.bottle_id)

    def test_attribute_wrong_token_denied(self) -> None:
        self.store.register("10.243.0.1")
        self.assertIsNone(self.store.attribute("10.243.0.1", "wrong-token"))

    def test_attribute_unknown_ip_denied(self) -> None:
        rec = self.store.register("10.243.0.1")
        self.assertIsNone(self.store.attribute("10.243.9.9", rec.identity_token))

    def test_attribute_empty_token_denied(self) -> None:
        self.store.register("10.243.0.1")
        self.assertIsNone(self.store.attribute("10.243.0.1", ""))

    def test_attribute_ambiguous_ip_denied(self) -> None:
        # Two active bottles on one source IP is a misconfiguration — deny
        # rather than guess (fail-closed), even with a valid token.
        a = self.store.register("10.243.0.1", bottle_id="a")
        self.store.register("10.243.0.1", bottle_id="b")
        self.assertIsNone(self.store.attribute("10.243.0.1", a.identity_token))

    def test_state_persists_across_reopen(self) -> None:
        rec = self.store.register("10.243.0.1")
        reopened = RegistryStore(self.db)
        got = reopened.get(rec.bottle_id)
        assert got is not None
        self.assertEqual(rec, got)
        # Attribution works against the reopened (durable) store too.
        self.assertIsNotNone(reopened.attribute("10.243.0.1", rec.identity_token))

    def test_redacted_hides_token(self) -> None:
        rec = BottleRecord(
            bottle_id="x", source_ip="10.243.0.1", identity_token="secret"
        )
        self.assertNotIn("identity_token", rec.redacted())
        self.assertEqual("10.243.0.1", rec.redacted()["source_ip"])


if __name__ == "__main__":
    unittest.main()
