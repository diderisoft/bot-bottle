"""Unit tests for the Orchestrator launch lifecycle (PRD 0070)."""

from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.orchestrator.broker import LaunchBroker, LaunchRequest, StubBroker
from bot_bottle.orchestrator.registry import RegistryStore
from bot_bottle.orchestrator.service import Orchestrator
from bot_bottle.orchestrator.gateway import Gateway
from bot_bottle.store_manager import StoreManager
from bot_bottle.supervise import (
    Proposal,
    STATUS_APPROVED,
    TOOL_EGRESS_ALLOW,
    read_response,
    sha256_hex,
    write_proposal,
)


class _FailingBroker(LaunchBroker):
    """Verifies the token like any broker, then fails the launch — to
    exercise the orchestrator's registry rollback."""

    def _launch(self, req: LaunchRequest) -> None:
        raise RuntimeError("launch failed")

    def _teardown(self, req: LaunchRequest) -> None:
        pass


class _FakeGateway(Gateway):
    """In-memory gateway for wiring tests."""

    def __init__(self) -> None:
        self.name = "fake-gateway"
        self.ensured = 0
        self.built = 0
        self._running = False

    def ensure_built(self) -> None:
        self.built += 1

    def ensure_running(self) -> None:
        self.ensured += 1
        self._running = True

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._running = False


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

    def test_launch_with_policy_is_resolvable(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.1", policy='{"routes":[]}')
        got = self.orch.attribute("10.243.0.1", rec.identity_token)
        assert got is not None
        self.assertEqual('{"routes":[]}', got.policy)

    def test_tokens_held_in_memory_and_cleared_on_teardown(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.5", tokens={"EGRESS_TOKEN_0": "sk"})
        self.assertEqual({"EGRESS_TOKEN_0": "sk"}, self.orch.tokens_for(rec.bottle_id))
        # not persisted in the registry (redacted record has no tokens field)
        self.assertNotIn("tokens", rec.redacted())
        self.orch.teardown_bottle(rec.bottle_id)
        self.assertEqual({}, self.orch.tokens_for(rec.bottle_id))

    def test_tokens_default_empty(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.6")
        self.assertEqual({}, self.orch.tokens_for(rec.bottle_id))

    def test_set_policy_live_reload(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.3")
        self.assertTrue(self.orch.set_policy(rec.bottle_id, '{"x":1}'))
        got = self.orch.attribute("10.243.0.3", rec.identity_token)
        assert got is not None
        self.assertEqual('{"x":1}', got.policy)

    def test_set_policy_unknown_is_false(self) -> None:
        self.assertFalse(self.orch.set_policy("ghost", "{}"))

    def test_resolve_requires_matching_token(self) -> None:
        # Mandatory (source_ip, token) pair — no source-IP-only fallback.
        rec = self.orch.launch_bottle("10.243.0.1", policy="P")
        self.assertIsNone(self.orch.resolve("10.243.0.1", ""))  # empty token denies
        self.assertIsNone(self.orch.resolve("10.243.0.1", "wrong"))  # mismatch denies
        got = self.orch.resolve("10.243.0.1", rec.identity_token)  # exact pair
        assert got is not None
        self.assertEqual(rec.bottle_id, got.bottle_id)
        self.assertEqual("P", got.policy)

    def test_resolve_with_token_stays_strict(self) -> None:
        rec = self.orch.launch_bottle("10.243.0.3")
        self.assertIsNotNone(self.orch.resolve("10.243.0.3", rec.identity_token))
        self.assertIsNone(self.orch.resolve("10.243.0.3", "wrong-token"))

    def test_launch_rolls_back_registry_on_broker_failure(self) -> None:
        orch = Orchestrator(self.store, _FailingBroker(self.secret), self.secret)
        with self.assertRaises(RuntimeError):
            orch.launch_bottle("10.243.0.9")
        self.assertEqual([], self.store.all())  # no orphan

    def test_gateway_unconfigured_by_default(self) -> None:
        self.assertEqual({"configured": False}, self.orch.gateway_status())
        self.orch.ensure_gateway()  # no-op, must not raise

    def test_gateway_wired_and_ensured(self) -> None:
        sc = _FakeGateway()
        orch = Orchestrator(self.store, self.broker, self.secret, gateway=sc)
        self.assertEqual(
            {"configured": True, "name": "fake-gateway", "running": False},
            orch.gateway_status(),
        )
        orch.ensure_gateway()
        self.assertEqual(1, sc.built)   # ensure_gateway builds first,
        self.assertEqual(1, sc.ensured)  # then runs
        self.assertEqual(
            {"configured": True, "name": "fake-gateway", "running": True},
            orch.gateway_status(),
        )


class TestOrchestratorSupervise(unittest.TestCase):
    """Operator-approval flow: the orchestrator applies the decision
    server-side against the single DB (queue + policy + audit)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        db = root / "db" / "bot-bottle.db"
        db.parent.mkdir(parents=True)
        # One DB for registry + supervise queue + audit (as in the VM).
        self._env = patch.dict("os.environ", {
            "BOT_BOTTLE_ROOT": str(root),
            "SUPERVISE_DB_PATH": str(db),
        })
        self._env.start()
        self.store = RegistryStore(db)
        self.store.migrate()
        StoreManager(db).migrate()
        secret = secrets.token_bytes(16)
        self.orch = Orchestrator(self.store, StubBroker(secret), secret)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _register(self, slug: str, policy: str) -> str:
        rec = self.store.register(
            "10.243.0.1", metadata=json.dumps({"slug": slug}), policy=policy)
        return rec.bottle_id

    def _queue(self, slug: str, proposed: str) -> str:
        p = Proposal.new(
            bottle_slug=slug, tool=TOOL_EGRESS_ALLOW, proposed_file=proposed,
            justification="need it", current_file_hash=sha256_hex(proposed))
        write_proposal(p)
        return p.id

    def test_pending_lists_queued_proposal(self) -> None:
        self._register("demo", "routes: []\n")
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        pending = self.orch.supervise_pending()
        self.assertEqual(1, len(pending))
        self.assertEqual(pid, pending[0]["id"])
        self.assertEqual("demo", pending[0]["bottle_slug"])

    def test_approve_applies_policy_writes_response_and_clears_pending(self) -> None:
        bottle_id = self._register("demo", "routes:\n  - host: existing.com\n")
        new_routes = "routes:\n  - host: google.com\n"
        pid = self._queue("demo", new_routes)
        ok, err = self.orch.supervise_respond(
            pid, bottle_slug="demo", decision="approve")
        self.assertTrue(ok, err)
        # policy live-applied so /resolve serves the new routes
        rec = self.store.get(bottle_id)
        assert rec is not None
        self.assertEqual(new_routes, rec.policy)
        # response written -> agent unblocks, proposal no longer pending
        self.assertEqual(STATUS_APPROVED, read_response("demo", pid).status)
        self.assertEqual([], self.orch.supervise_pending())

    def test_modify_applies_final_file_not_proposed(self) -> None:
        bottle_id = self._register("demo", "routes: []\n")
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        edited = "routes:\n  - host: example.com\n"
        ok, _ = self.orch.supervise_respond(
            pid, bottle_slug="demo", decision="modify", final_file=edited)
        self.assertTrue(ok)
        rec = self.store.get(bottle_id)
        assert rec is not None
        self.assertEqual(edited, rec.policy)

    def test_reject_leaves_policy_unchanged(self) -> None:
        bottle_id = self._register("demo", "routes:\n  - host: existing.com\n")
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        ok, _ = self.orch.supervise_respond(
            pid, bottle_slug="demo", decision="reject", notes="no")
        self.assertTrue(ok)
        rec = self.store.get(bottle_id)
        assert rec is not None
        self.assertEqual("routes:\n  - host: existing.com\n", rec.policy)
        self.assertEqual("rejected", read_response("demo", pid).status)

    def test_unknown_proposal_is_error(self) -> None:
        ok, err = self.orch.supervise_respond(
            "ghost", bottle_slug="demo", decision="approve")
        self.assertFalse(ok)
        self.assertIn("no such proposal", err)

    def test_unknown_decision_is_error(self) -> None:
        self._register("demo", "routes: []\n")
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        ok, err = self.orch.supervise_respond(
            pid, bottle_slug="demo", decision="bogus")
        self.assertFalse(ok)
        self.assertIn("unknown decision", err)

    def test_approve_when_bottle_gone_cannot_apply(self) -> None:
        # Proposal queued but the bottle was torn down before the operator
        # acted: an egress apply has no target, so respond fails closed.
        pid = self._queue("ghost-bottle", "routes:\n  - host: google.com\n")
        ok, err = self.orch.supervise_respond(
            pid, bottle_slug="ghost-bottle", decision="approve")
        self.assertFalse(ok)
        self.assertIn("no longer registered", err)


if __name__ == "__main__":
    unittest.main()
