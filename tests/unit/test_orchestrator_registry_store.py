"""Unit tests for the orchestrator bottle registry + attribution (PRD 0070)."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from bot_bottle.orchestrator.store.registry_store import (
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

    def _force_active_row(self, source_ip: str, bottle_id: str) -> None:
        """Insert a raw active row, bypassing `register`'s same-IP supersede —
        the only way to manufacture the ambiguity the fail-closed guard exists
        for (crash-orphaned / externally-injected duplicate)."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO orchestrator_bottles "
            "(bottle_id, source_ip, identity_token, state, created_at, metadata, policy) "
            "VALUES (?, ?, ?, 'active', 0.0, '', '')",
            (bottle_id, source_ip, "tok-" + bottle_id),
        )
        conn.commit()
        conn.close()

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
        # rather than guess (fail-closed), even with a valid token. (Forced
        # directly: `register` now supersedes, so this can only arise from an
        # orphaned/injected row.)
        a = self.store.register("10.243.0.1", bottle_id="a")
        self._force_active_row("10.243.0.1", "b")
        self.assertIsNone(self.store.attribute("10.243.0.1", a.identity_token))

    def test_reregister_same_source_ip_supersedes(self) -> None:
        # Re-registering a source IP (reused IP / crash recovery / persisted
        # DB) must supersede the prior active bottle, not add a second active
        # row that would fail-closed the whole IP to 403.
        a = self.store.register("10.243.0.1", bottle_id="a", policy="A")
        b = self.store.register("10.243.0.1", bottle_id="b", policy="B")
        # exactly one active row at that IP, and it's the new bottle
        got = self.store.by_source_ip("10.243.0.1")
        assert got is not None
        self.assertEqual("b", got.bottle_id)
        self.assertEqual("B", got.policy)
        # the superseded bottle is gone and its token no longer attributes
        self.assertIsNone(self.store.get("a"))
        self.assertIsNone(self.store.attribute("10.243.0.1", a.identity_token))
        self.assertIsNotNone(self.store.attribute("10.243.0.1", b.identity_token))

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

    def test_register_with_policy_resolves_via_attribution(self) -> None:
        rec = self.store.register("10.243.0.1", policy='{"allow":["x"]}')
        self.assertEqual('{"allow":["x"]}', rec.policy)
        got = self.store.attribute("10.243.0.1", rec.identity_token)
        assert got is not None
        self.assertEqual('{"allow":["x"]}', got.policy)

    def test_set_policy_updates_live(self) -> None:
        rec = self.store.register("10.243.0.1")
        self.assertEqual("", rec.policy)
        self.assertTrue(self.store.set_policy(rec.bottle_id, '{"v":2}'))
        got = self.store.get(rec.bottle_id)
        assert got is not None
        self.assertEqual('{"v":2}', got.policy)

    def test_set_policy_missing_is_false(self) -> None:
        self.assertFalse(self.store.set_policy("ghost", "{}"))

    def test_policy_defaults_empty_and_persists(self) -> None:
        rec = self.store.register("10.243.0.1")
        got = RegistryStore(self.db).get(rec.bottle_id)
        assert got is not None
        self.assertEqual("", got.policy)

    def test_by_source_ip_returns_single_active(self) -> None:
        rec = self.store.register("10.243.0.1")
        got = self.store.by_source_ip("10.243.0.1")
        assert got is not None
        self.assertEqual(rec.bottle_id, got.bottle_id)

    def test_by_source_ip_unknown_or_ambiguous_denied(self) -> None:
        self.assertIsNone(self.store.by_source_ip("10.243.9.9"))  # unknown
        self.store.register("10.243.0.1", bottle_id="a")
        self._force_active_row("10.243.0.1", "b")  # orphaned duplicate
        self.assertIsNone(self.store.by_source_ip("10.243.0.1"))  # ambiguous


if __name__ == "__main__":
    unittest.main()


class TestAgentSecrets(unittest.TestCase):
    """store/get/delete for the bottled_agent_secrets table."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "registry.db"
        self.store = RegistryStore(self.db)
        self.store.migrate()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_store_and_get_roundtrip(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"EGRESS_TOKEN_1": "enc-val-a"})
        got = self.store.get_agent_secrets("bottle-1")
        self.assertEqual({"EGRESS_TOKEN_1": "enc-val-a"}, got)

    def test_get_returns_empty_when_none_stored(self) -> None:
        self.assertEqual({}, self.store.get_agent_secrets("no-such-bottle"))

    def test_store_replaces_existing_rows(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"K": "old"})
        self.store.store_agent_secrets("bottle-1", {"K": "new", "K2": "v2"})
        got = self.store.get_agent_secrets("bottle-1")
        self.assertEqual({"K": "new", "K2": "v2"}, got)

    def test_store_agent_secret_upserts_one_key_leaving_others(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"K": "v1", "K2": "v2"})
        self.store.store_agent_secret("bottle-1", "K", "v1-new")  # update existing
        self.store.store_agent_secret("bottle-1", "K3", "v3")     # insert new
        self.assertEqual(
            {"K": "v1-new", "K2": "v2", "K3": "v3"},
            self.store.get_agent_secrets("bottle-1"),
        )

    def test_store_agent_secret_does_not_duplicate_rows(self) -> None:
        self.store.store_agent_secret("bottle-1", "K", "v1")
        self.store.store_agent_secret("bottle-1", "K", "v2")
        self.assertEqual({"K": "v2"}, self.store.get_agent_secrets("bottle-1"))

    def test_delete_removes_secrets(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"K": "v"})
        self.store.delete_agent_secrets("bottle-1")
        self.assertEqual({}, self.store.get_agent_secrets("bottle-1"))

    def test_delete_is_idempotent_on_missing(self) -> None:
        self.store.delete_agent_secrets("no-such-bottle")  # must not raise

    def test_secrets_are_isolated_by_bottle_id(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"K": "for-1"})
        self.store.store_agent_secrets("bottle-2", {"K": "for-2"})
        self.assertEqual({"K": "for-1"}, self.store.get_agent_secrets("bottle-1"))
        self.assertEqual({"K": "for-2"}, self.store.get_agent_secrets("bottle-2"))

    def test_secrets_isolated_by_type(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"K": "injected"}, secret_type="injected_env_var")
        self.store.store_agent_secrets("bottle-1", {"K": "other"}, secret_type="other_type")
        self.assertEqual({"K": "injected"}, self.store.get_agent_secrets("bottle-1"))
        self.assertEqual({"K": "other"}, self.store.get_agent_secrets("bottle-1", secret_type="other_type"))

    def test_secrets_persist_across_reopen(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"K": "v"})
        reopened = RegistryStore(self.db)
        self.assertEqual({"K": "v"}, reopened.get_agent_secrets("bottle-1"))

    def test_v6_migration_clears_legacy_secret_rows(self) -> None:
        self.store.store_agent_secrets("bottle-1", {"K": "legacy"})
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE schema_versions SET version = 5 "
                "WHERE module = 'orchestrator_registry'"
            )
            conn.commit()
        self.store.migrate()
        self.assertEqual({}, self.store.get_agent_secrets("bottle-1"))


class TestReapAbsent(unittest.TestCase):
    """`reap_absent` — the self-heal for rows whose bottle is gone.

    An orphan is not merely untidy: source IPs get recycled, and
    `by_source_ip` fail-closes on ambiguity, so a leftover row at a reused
    address resolves *no* policy for the next bottle that lands there and
    every host it asks for is denied.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "registry.db"
        self.store = RegistryStore(self.db)
        self.store.migrate()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _aged(self, source_ip: str, *, age: float) -> BottleRecord:
        """Register a bottle and backdate it past the grace window."""
        rec = self.store.register(source_ip)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE orchestrator_bottles SET created_at = ? WHERE bottle_id = ?",
                (time.time() - age, rec.bottle_id),
            )
            conn.commit()
        return rec

    def test_reaps_row_with_no_live_container(self) -> None:
        gone = self._aged("10.243.0.9", age=600)
        reaped = self.store.reap_absent([])
        self.assertEqual([gone.bottle_id], [r.bottle_id for r in reaped])
        self.assertIsNone(self.store.get(gone.bottle_id))

    def test_keeps_row_whose_ip_is_live(self) -> None:
        alive = self._aged("10.243.0.9", age=600)
        self.assertEqual([], self.store.reap_absent(["10.243.0.9"]))
        self.assertIsNotNone(self.store.get(alive.bottle_id))

    def test_grace_window_protects_an_in_flight_launch(self) -> None:
        """A bottle registered moments ago is never reaped, even though the
        caller's enumeration didn't see its address yet."""
        fresh = self.store.register("10.243.0.10")
        self.assertEqual([], self.store.reap_absent([]))
        self.assertIsNotNone(self.store.get(fresh.bottle_id))

    def test_reaping_the_orphan_unbricks_the_reused_address(self) -> None:
        """The regression this exists for: an orphan at an address that vmnet
        later hands to a new bottle makes `by_source_ip` ambiguous, so the new
        bottle resolves no policy at all."""
        orphan = self._aged("10.243.0.11", age=600)
        # A new bottle lands on the recycled address. Force the row in directly
        # so `register`'s own supersede sweep doesn't mask the ambiguity.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "INSERT INTO orchestrator_bottles "
                "(bottle_id, source_ip, identity_token, state, created_at, metadata, policy) "
                "VALUES ('newbottle', '10.243.0.11', 'tok-new', 'active', ?, '', 'routes: []')",
                (time.time(),),
            )
            conn.commit()
        self.assertIsNone(self.store.by_source_ip("10.243.0.11"))  # bricked

        reaped = self.store.reap_absent(["10.243.0.11"], grace_seconds=60)
        self.assertEqual([orphan.bottle_id], [r.bottle_id for r in reaped])
        rec = self.store.by_source_ip("10.243.0.11")
        assert rec is not None
        self.assertEqual("newbottle", rec.bottle_id)

    def test_ignores_empty_ips_in_the_live_set(self) -> None:
        gone = self._aged("10.243.0.12", age=600)
        self.assertEqual(
            [gone.bottle_id],
            [r.bottle_id for r in self.store.reap_absent(["", "10.243.0.99"])],
        )
