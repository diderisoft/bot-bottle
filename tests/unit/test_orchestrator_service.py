"""Unit tests for the OrchestratorCore registration lifecycle (PRD 0070)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bot_bottle.orchestrator.store.registry_store import RegistryStore
from bot_bottle.orchestrator.service import OrchestratorCore
from bot_bottle.orchestrator.store.secret_store import new_env_var_secret
from bot_bottle.orchestrator.store.store_manager import StoreManager
from bot_bottle.contract.supervise import (
    Proposal,
    STATUS_APPROVED,
    TOOL_EGRESS_ALLOW,
)
from bot_bottle.orchestrator.supervisor import Supervisor
from bot_bottle.orchestrator.hostctl_relay import LiveBottlesUnknown


class _Live:
    """The host controller's live set (stands in for `HostControlRelay`)."""

    def __init__(self, *ips: str, error: str = "") -> None:
        self.ips = list(ips)
        self.error = error

    def live_source_ips(self) -> list[str]:
        if self.error:
            raise LiveBottlesUnknown(self.error)
        return self.ips


class TestOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RegistryStore(Path(self._tmp.name) / "r.db")
        self.store.migrate()
        StoreManager(self.store.db_path).migrate()
        self.orch = OrchestratorCore(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_register_records_the_bottle(self) -> None:
        rec = self.orch.register_bottle("10.243.0.1")
        got = self.store.get(rec.bottle_id)
        assert got is not None
        self.assertEqual("10.243.0.1", got.source_ip)

    def test_launch_then_attribute(self) -> None:
        rec = self.orch.register_bottle("10.243.0.3")
        got = self.orch.attribute("10.243.0.3", rec.identity_token)
        assert got is not None
        self.assertEqual(rec.bottle_id, got.bottle_id)
        self.assertIsNone(self.orch.attribute("10.243.0.3", "wrong-token"))

    def test_deregister_drops_the_bottle(self) -> None:
        rec = self.orch.register_bottle("10.243.0.1")
        self.assertTrue(self.orch.deregister_bottle(rec.bottle_id))
        self.assertIsNone(self.store.get(rec.bottle_id))

    def test_deregister_unknown_is_false(self) -> None:
        self.assertFalse(self.orch.deregister_bottle("ghost"))

    def test_launch_with_policy_is_resolvable(self) -> None:
        rec = self.orch.register_bottle("10.243.0.1", policy='{"routes":[]}')
        got = self.orch.attribute("10.243.0.1", rec.identity_token)
        assert got is not None
        self.assertEqual('{"routes":[]}', got.policy)

    def test_tokens_held_in_memory_and_cleared_on_teardown(self) -> None:
        rec = self.orch.register_bottle("10.243.0.5", tokens={"EGRESS_TOKEN_0": "sk"})
        self.assertEqual({"EGRESS_TOKEN_0": "sk"}, self.orch.tokens_for(rec.bottle_id))
        # not persisted in the registry (redacted record has no tokens field)
        self.assertNotIn("tokens", rec.redacted())
        self.orch.deregister_bottle(rec.bottle_id)
        self.assertEqual({}, self.orch.tokens_for(rec.bottle_id))

    def test_tokens_default_empty(self) -> None:
        rec = self.orch.register_bottle("10.243.0.6")
        self.assertEqual({}, self.orch.tokens_for(rec.bottle_id))

    def test_encrypted_tokens_can_be_reprovisioned_after_memory_loss(self) -> None:
        key = new_env_var_secret()
        rec = self.orch.register_bottle(
            "10.243.0.12", tokens={"EGRESS_TOKEN_0": "secret"},
            env_var_secret=key,
        )
        self.assertNotEqual({}, self.store.get_agent_secrets(rec.bottle_id))
        self.orch._tokens.clear()
        self.assertTrue(self.orch.reprovision_from_secret(rec.bottle_id, key))
        self.assertEqual({"EGRESS_TOKEN_0": "secret"}, self.orch.tokens_for(rec.bottle_id))

    def test_update_agent_secret_refreshes_one_token_in_place(self) -> None:
        key = new_env_var_secret()
        rec = self.orch.register_bottle(
            "10.243.0.20", tokens={"A": "old-a", "B": "keep-b"}, env_var_secret=key,
        )
        self.assertTrue(self.orch.update_agent_secret(rec.bottle_id, "A", "new-a", key))
        # In memory: A refreshed, B left untouched.
        self.assertEqual({"A": "new-a", "B": "keep-b"}, self.orch.tokens_for(rec.bottle_id))
        # At rest: the whole set stays decryptable with the SAME env_var_secret,
        # so a later reprovision restores the refreshed value (not the launch one).
        self.orch._tokens.clear()
        self.assertTrue(self.orch.reprovision_from_secret(rec.bottle_id, key))
        self.assertEqual({"A": "new-a", "B": "keep-b"}, self.orch.tokens_for(rec.bottle_id))

    def test_update_agent_secret_unknown_bottle_is_false(self) -> None:
        self.assertFalse(
            self.orch.update_agent_secret("ghost", "A", "v", new_env_var_secret())
        )

    def test_reprovision_rejects_missing_rows_and_wrong_key(self) -> None:
        self.assertFalse(self.orch.reprovision_from_secret("missing", new_env_var_secret()))
        key = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
        wrong_key = "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQ"
        # Pin the nonce so this is a deterministic wrong-key/decryption vector
        # instead of a probabilistic assertion over random bytes.
        with patch(
            "bot_bottle.orchestrator.store.secret_store.secrets.token_bytes",
            return_value=b"\0" * 16,
        ):
            rec = self.orch.register_bottle(
                "10.243.0.13", tokens={"K": "value"},
                env_var_secret=key,
            )
        self.assertFalse(self.orch.reprovision_from_secret(rec.bottle_id, wrong_key))

    def test_set_policy_live_reload(self) -> None:
        rec = self.orch.register_bottle("10.243.0.3")
        self.assertTrue(self.orch.set_policy(rec.bottle_id, '{"x":1}'))
        got = self.orch.attribute("10.243.0.3", rec.identity_token)
        assert got is not None
        self.assertEqual('{"x":1}', got.policy)

    def test_set_policy_unknown_is_false(self) -> None:
        self.assertFalse(self.orch.set_policy("ghost", "{}"))

    def test_resolve_requires_matching_token(self) -> None:
        # Mandatory (source_ip, token) pair — no source-IP-only fallback.
        rec = self.orch.register_bottle("10.243.0.1", policy="P")
        self.assertIsNone(self.orch.resolve("10.243.0.1", ""))  # empty token denies
        self.assertIsNone(self.orch.resolve("10.243.0.1", "wrong"))  # mismatch denies
        got = self.orch.resolve("10.243.0.1", rec.identity_token)  # exact pair
        assert got is not None
        self.assertEqual(rec.bottle_id, got.bottle_id)
        self.assertEqual("P", got.policy)

    def test_resolve_with_token_stays_strict(self) -> None:
        rec = self.orch.register_bottle("10.243.0.3")
        self.assertIsNotNone(self.orch.resolve("10.243.0.3", rec.identity_token))
        self.assertIsNone(self.orch.resolve("10.243.0.3", "wrong-token"))

    def test_gateway_status_reports_unconfigured(self) -> None:
        # The orchestrator no longer owns a standalone gateway lifecycle; the
        # consolidated flow runs the gateway data plane in the per-host infra
        # container/VM. `GET /gateway` therefore always reports unconfigured.
        self.assertEqual({"configured": False}, self.orch.gateway_status())


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
        })
        self._env.start()
        self.db = db
        self.store = RegistryStore(db)
        self.store.migrate()
        StoreManager(db).migrate()
        self.orch = OrchestratorCore(self.store)

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
            justification="need it")
        Supervisor(self.db).write_proposal(p)
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
        self.assertEqual(STATUS_APPROVED, Supervisor(self.db).read_response("demo", pid).status)
        self.assertEqual([], self.orch.supervise_pending())

    def test_pending_carries_human_label(self) -> None:
        # The proposal is keyed by bottle_id, but pending dicts also expose the
        # bottle's human slug so the operator sees a name, not a hex id.
        bottle_id = self._register("codex-dev-a1b2c", "routes: []\n")
        self._queue(bottle_id, "routes:\n  - host: google.com\n")
        pending = self.orch.supervise_pending()
        self.assertEqual(bottle_id, pending[0]["bottle_slug"])
        self.assertEqual("codex-dev-a1b2c", pending[0]["bottle_label"])

    def test_pending_carries_the_policy_it_would_replace(self) -> None:
        bottle_id = self._register("demo-a1b2c", "routes:\n  - host: existing.com\n")
        self._queue(bottle_id, "routes:\n  - host: google.com\n")
        self.assertEqual("routes:\n  - host: existing.com\n",
                         self.orch.supervise_pending()[0]["current_file"])

    def test_pending_label_falls_back_to_slug_when_bottle_gone(self) -> None:
        # No registry record (torn down): label is the id, never empty.
        self._queue("ghost-id", "routes:\n  - host: google.com\n")
        pending = self.orch.supervise_pending()
        self.assertEqual("ghost-id", pending[0]["bottle_label"])

    def test_approve_by_bottle_id_applies_policy(self) -> None:
        # Consolidated reality: the supervise server keys each proposal by the
        # orchestrator-assigned bottle_id, not the human slug. Approval must
        # resolve the record by that id and apply the policy (regression for
        # the "bottle <id> is no longer registered" 409).
        bottle_id = self._register("codex-dev-a1b2c", "routes: []\n")
        new_routes = "routes:\n  - host: google.com\n"
        pid = self._queue(bottle_id, new_routes)
        ok, err = self.orch.supervise_respond(
            pid, bottle_slug=bottle_id, decision="approve")
        self.assertTrue(ok, err)
        rec = self.store.get(bottle_id)
        assert rec is not None
        self.assertEqual(new_routes, rec.policy)

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
        self.assertEqual("rejected", Supervisor(self.db).read_response("demo", pid).status)

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

    # --- agent half: queue + poll (issue #469) -----------------------------

    def test_queue_proposal_then_poll_pending(self) -> None:
        bottle_id = self._register("demo", "routes: []\n")
        pid = self.orch.supervise_queue_proposal(
            bottle_id, tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: google.com\n", justification="need it")
        # Visible to the operator, keyed by the bottle id.
        pending = self.orch.supervise_pending()
        self.assertEqual([pid], [p["id"] for p in pending])
        self.assertEqual(
            {"status": "pending"}, self.orch.supervise_poll_response(bottle_id, pid))

    def test_poll_is_idempotent_and_leaves_no_pending(self) -> None:
        bottle_id = self._register("demo", "routes: []\n")
        pid = self.orch.supervise_queue_proposal(
            bottle_id, tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: google.com\n", justification="need it")
        self.orch.supervise_respond(
            pid, bottle_slug=bottle_id, decision="approve", notes="ok")
        decided = self.orch.supervise_poll_response(bottle_id, pid)
        self.assertEqual("approved", decided["status"])
        self.assertEqual("ok", decided["notes"])
        # Decided proposal drops off the operator's pending list (a response row
        # exists), but poll does NOT archive — a re-poll returns the same
        # decision so a dropped connection can't lose it (issue #469 review).
        self.assertEqual([], self.orch.supervise_pending())
        self.assertEqual(decided, self.orch.supervise_poll_response(bottle_id, pid))

    def test_teardown_reaps_the_bottles_proposals(self) -> None:
        rec = self.store.register("10.243.0.20", policy="routes: []\n")
        pid = self.orch.supervise_queue_proposal(
            rec.bottle_id, tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: google.com\n", justification="j")
        self.orch.supervise_respond(
            pid, bottle_slug=rec.bottle_id, decision="approve", notes="ok")
        self.assertTrue(self.orch.deregister_bottle(rec.bottle_id))
        # The gone bottle's decided-but-unconsumed proposal is archived, so a
        # late poll returns 'unknown' rather than lingering forever.
        self.assertEqual(
            {"status": "unknown"}, self.orch.supervise_poll_response(rec.bottle_id, pid))

    def test_reconcile_reaps_the_bottles_proposals(self) -> None:
        rec = self.store.register("10.243.0.21", policy="routes: []\n")
        pid = self.orch.supervise_queue_proposal(
            rec.bottle_id, tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: google.com\n", justification="j")
        # No live source IPs -> the bottle is reaped (grace 0 so it's immediate).
        self.assertEqual([rec.bottle_id], self.orch.reconcile(_Live(), grace_seconds=0))
        self.assertEqual(
            {"status": "unknown"}, self.orch.supervise_poll_response(rec.bottle_id, pid))

    def test_poll_unknown_for_other_bottle(self) -> None:
        bottle_id = self._register("demo", "routes: []\n")
        pid = self.orch.supervise_queue_proposal(
            bottle_id, tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: google.com\n", justification="j")
        # A different bottle id can't read demo's proposal (scoped by queue key).
        self.assertEqual(
            {"status": "unknown"}, self.orch.supervise_poll_response("other-bottle", pid))


if __name__ == "__main__":
    unittest.main()


class TestOrchestratorReconcile(unittest.TestCase):
    """`reconcile` — drop rows for bottles that are no longer running."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "r.db"
        self.store = RegistryStore(self.db)
        self.store.migrate()
        StoreManager(self.db).migrate()
        self.orch = OrchestratorCore(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _age_all(self, seconds: float) -> None:
        """Backdate every row past the reap grace window."""
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE orchestrator_bottles SET created_at = created_at - ?", (seconds,))
            conn.commit()

    def test_reaps_dead_bottle_and_forgets_its_tokens(self) -> None:
        dead = self.orch.register_bottle("10.243.0.1", tokens={"EGRESS_TOKEN_0": "s3cret"})
        live = self.orch.register_bottle("10.243.0.2", tokens={"EGRESS_TOKEN_0": "keep"})
        self._age_all(600)

        self.assertEqual([dead.bottle_id], self.orch.reconcile(_Live("10.243.0.2")))
        self.assertIsNone(self.store.get(dead.bottle_id))
        self.assertIsNotNone(self.store.get(live.bottle_id))
        # The in-memory egress credential goes with the row.
        self.assertEqual({}, self.orch.tokens_for(dead.bottle_id))
        self.assertEqual({"EGRESS_TOKEN_0": "keep"}, self.orch.tokens_for(live.bottle_id))

    def test_reconcile_keeps_everything_when_all_are_live(self) -> None:
        a = self.orch.register_bottle("10.243.0.1")
        b = self.orch.register_bottle("10.243.0.2")
        self._age_all(600)
        self.assertEqual([], self.orch.reconcile(_Live("10.243.0.1", "10.243.0.2")))
        self.assertIsNotNone(self.store.get(a.bottle_id))
        self.assertIsNotNone(self.store.get(b.bottle_id))

    def test_no_live_set_reaps_nothing(self) -> None:
        """No host controller, or one that can't answer, is never read as
        "nothing is running": reconcile logs, raises and keeps every row."""
        rec = self.orch.register_bottle("10.243.0.1", tokens={"EGRESS_TOKEN_0": "keep"})
        self._age_all(600)
        for source in (None, _Live(error="host controller unreachable")):
            with self.subTest(source=source), \
                    patch("bot_bottle.orchestrator.service.log.warn") as warn:
                with self.assertRaises(LiveBottlesUnknown):
                    self.orch.reconcile(source, grace_seconds=0)
                self.assertIn("reconciliation skipped", warn.call_args.args[0])
        self.assertIsNotNone(self.store.get(rec.bottle_id))
        self.assertEqual({"EGRESS_TOKEN_0": "keep"}, self.orch.tokens_for(rec.bottle_id))
