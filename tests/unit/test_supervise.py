"""Unit: supervise queue + audit log + diff helpers (PRD 0013)."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from bot_bottle.backend.resolve_common import prepare_supervise
from bot_bottle.manifest import ManifestBottle
from bot_bottle.contract import supervise
from bot_bottle.paths import host_db_path
from bot_bottle.lib.text import render_diff, sha256_hex
from tests.unit import use_bottle_root
from bot_bottle.orchestrator.store.audit_store import AuditStore
from bot_bottle.orchestrator.store.queue_store import QueueStore
from bot_bottle.contract.supervise import (
    AuditEntry,
    Proposal,
    Response,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_EGRESS_ALLOW,
    TOOL_GITLEAKS_ALLOW,
)
from bot_bottle.orchestrator.supervisor import Supervisor

def _supervisor() -> Supervisor:
    """A `Supervisor` on the host DB under the current (test) root."""
    return Supervisor(host_db_path())


FIXED_TS = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(
    tool: str = TOOL_EGRESS_ALLOW,
    proposed: str = "routes:\n  - host: example.com\n",
    justification: str = "need egress",
) -> Proposal:
    return Proposal.new(
        bottle_slug="dev",
        tool=tool,
        proposed_file=proposed,
        justification=justification,
        now=FIXED_TS,
    )


class TestProposalRoundtrip(unittest.TestCase):
    def test_new_stamps_uuid_and_iso_timestamp(self):
        p = _proposal()
        self.assertTrue(p.id)
        self.assertEqual("2026-05-25T12:00:00+00:00", p.arrival_timestamp)
        self.assertEqual("dev", p.bottle_slug)
        self.assertEqual(TOOL_EGRESS_ALLOW, p.tool)

    def test_to_from_dict_roundtrip(self):
        p = _proposal()
        self.assertEqual(p, Proposal.from_dict(p.to_dict()))

    def test_from_dict_rejects_unknown_tool(self):
        raw = _proposal().to_dict()
        raw["tool"] = "not-a-real-tool"
        with self.assertRaises(ValueError):
            Proposal.from_dict(raw)

    def test_from_dict_rejects_missing_field(self):
        raw = _proposal().to_dict()
        del raw["justification"]
        with self.assertRaises(ValueError):
            Proposal.from_dict(raw)


class TestResponseRoundtrip(unittest.TestCase):
    def test_to_from_dict_approved(self):
        r = Response(proposal_id="abc", status=STATUS_APPROVED, notes="lgtm")
        self.assertEqual(r, Response.from_dict(r.to_dict()))

    def test_to_from_dict_modified_with_final_file(self):
        r = Response(
            proposal_id="abc",
            status=STATUS_MODIFIED,
            notes="tweaked the upstream",
            final_file='{"routes": []}\n',
        )
        self.assertEqual(r, Response.from_dict(r.to_dict()))

    def test_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            Response.from_dict({
                "proposal_id": "abc",
                "status": "maybe",
                "notes": "",
                "final_file": None,
            })

    def test_rejects_non_string_final_file(self):
        with self.assertRaises(ValueError):
            Response.from_dict({
                "proposal_id": "abc",
                "status": STATUS_APPROVED,
                "notes": "",
                "final_file": 123,
            })


class TestQueueIO(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-supervise-test.")
        self._home_patch = self._patch_home(Path(self._tmp.name))
        self.slug = "dev"
        QueueStore("", host_db_path()).migrate()

    def tearDown(self):
        self._home_patch()
        self._tmp.cleanup()

    def _patch_home(self, fake_home: Path):
        return use_bottle_root(fake_home / ".bot-bottle")

    def test_write_and_read_proposal(self):
        p = _proposal()
        path = _supervisor().write_proposal(p)
        self.assertTrue(path.exists())
        self.assertEqual(host_db_path(), path)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        loaded = _supervisor().read_proposal(self.slug, p.id)
        self.assertEqual(p, loaded)

    def test_list_pending_excludes_responded(self):
        a = _proposal(justification="first")
        b = _proposal(justification="second")
        _supervisor().write_proposal(a)
        _supervisor().write_proposal(b)
        _supervisor().write_response(self.slug, Response(
            proposal_id=a.id, status=STATUS_APPROVED, notes="",
        ))
        pending = _supervisor().list_pending_proposals(self.slug)
        self.assertEqual([b.id], [p.id for p in pending])

    def test_list_pending_returns_empty_for_missing_slug(self):
        self.assertEqual([], _supervisor().list_pending_proposals("nope"))

    def test_list_pending_sorted_by_arrival(self):
        # Fabricate two with explicit timestamps.
        a = Proposal.new(
            bottle_slug="dev", tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: early.example.com\n", justification="early",
            now=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc),
        )
        b = Proposal.new(
            bottle_slug="dev", tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: late.example.com\n", justification="late",
            now=datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc),
        )
        # Write in reverse order.
        _supervisor().write_proposal(b)
        _supervisor().write_proposal(a)
        ordered = _supervisor().list_pending_proposals(self.slug)
        self.assertEqual([a.id, b.id], [p.id for p in ordered])

    def test_write_and_read_response(self):
        r = Response(proposal_id="xyz", status=STATUS_REJECTED, notes="no")
        _supervisor().write_response(self.slug, r)
        self.assertEqual(r, _supervisor().read_response(self.slug, "xyz"))


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-supervise-audit.")
        self._home_patch = self._patch_home(Path(self._tmp.name))
        AuditStore(host_db_path()).migrate()

    def tearDown(self):
        self._home_patch()
        self._tmp.cleanup()

    def _patch_home(self, fake_home: Path):
        return use_bottle_root(fake_home / ".bot-bottle")

    def test_write_then_read_single_entry(self):
        e = AuditEntry(
            timestamp="2026-05-25T12:00:00+00:00",
            bottle_slug="dev",
            component="cred-proxy",
            operator_action=STATUS_APPROVED,
            operator_notes="lgtm",
            justification="agent needed gh-api token",
            diff="--- before\n+++ after\n",
        )
        path = _supervisor().write_audit_entry(e)
        self.assertEqual(host_db_path(), path)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        loaded = _supervisor().read_audit_entries("cred-proxy", "dev")
        self.assertEqual([e], loaded)

    def test_appends_one_line_per_entry(self):
        for i in range(3):
            _supervisor().write_audit_entry(AuditEntry(
                timestamp=f"2026-05-25T12:00:0{i}+00:00",
                bottle_slug="dev",
                component="egress",
                operator_action=STATUS_APPROVED,
                operator_notes=f"n{i}",
                justification="",
                diff="",
            ))
        entries = _supervisor().read_audit_entries("egress", "dev")
        self.assertEqual(3, len(entries))
        self.assertEqual(
            ["2026-05-25T12:00:00+00:00", "2026-05-25T12:00:01+00:00",
             "2026-05-25T12:00:02+00:00"],
            [entry.timestamp for entry in entries],
        )

    def test_separate_logs_per_component_slug(self):
        _supervisor().write_audit_entry(AuditEntry(
            timestamp="t",
            bottle_slug="dev",
            component="cred-proxy",
            operator_action=STATUS_APPROVED,
            operator_notes="",
            justification="",
            diff="",
        ))
        _supervisor().write_audit_entry(AuditEntry(
            timestamp="t",
            bottle_slug="dev",
            component="egress",
            operator_action=STATUS_APPROVED,
            operator_notes="",
            justification="",
            diff="",
        ))
        _supervisor().write_audit_entry(AuditEntry(
            timestamp="t",
            bottle_slug="other",
            component="cred-proxy",
            operator_action=STATUS_REJECTED,
            operator_notes="",
            justification="",
            diff="",
        ))
        self.assertEqual(1, len(_supervisor().read_audit_entries("cred-proxy", "dev")))
        self.assertEqual(1, len(_supervisor().read_audit_entries("egress", "dev")))
        self.assertEqual(1, len(_supervisor().read_audit_entries("cred-proxy", "other")))

    def test_read_audit_entries_missing_log_returns_empty(self):
        self.assertEqual([], _supervisor().read_audit_entries("cred-proxy", "no-such-bottle"))


class TestDiffAndHash(unittest.TestCase):
    def test_render_diff_returns_empty_when_unchanged(self):
        self.assertEqual("", render_diff("a\nb\n", "a\nb\n", before_title="current", after_label="proposed"))

    def test_render_diff_shows_changes(self):
        diff = render_diff("a\nb\nc\n", "a\nB\nc\n", label="routes.yaml", before_title="current", after_label="proposed")
        self.assertIn("routes.yaml (current)", diff)
        self.assertIn("routes.yaml (proposed)", diff)
        self.assertIn("-b", diff)
        self.assertIn("+B", diff)

    def test_sha256_hex_is_deterministic_and_hex(self):
        h1 = sha256_hex("hello")
        h2 = sha256_hex("hello")
        self.assertEqual(h1, h2)
        self.assertEqual(64, len(h1))
        int(h1, 16)  # parses as hex


class TestToolConstants(unittest.TestCase):
    def test_tools_tuple_matches_individual_constants(self):
        self.assertEqual(
            (
                supervise.TOOL_EGRESS_ALLOW,
                supervise.TOOL_EGRESS_BLOCK,
                TOOL_GITLEAKS_ALLOW,
                supervise.TOOL_EGRESS_TOKEN_ALLOW,
                supervise.TOOL_LIST_EGRESS_ROUTES,
                supervise.TOOL_FORGE_CONNECT,
                supervise.TOOL_ADD_REPO,
                supervise.TOOL_LAUNCH_BOTTLE,
                supervise.TOOL_CREATE_REPO,
            ),
            supervise.TOOLS,
        )

    def test_token_allow_proposal_roundtrips(self):
        p = Proposal.new(
            bottle_slug="dev",
            tool=supervise.TOOL_EGRESS_TOKEN_ALLOW,
            proposed_file="host: api.example.com\n",
            justification="false positive",
        )
        self.assertEqual(p, Proposal.from_dict(p.to_dict()))

    def test_component_map_has_egress_entries(self):
        self.assertEqual(
            {
                supervise.TOOL_EGRESS_ALLOW: "egress",
                supervise.TOOL_EGRESS_BLOCK: "egress",
            },
            supervise.COMPONENT_FOR_TOOL,
        )


_SUPERVISED = cast(ManifestBottle, SimpleNamespace(supervise=True))


class TestSupervisePrepare(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-prepare-test.")
        self._home_patch = self._patch_home(Path(self._tmp.name))
        self.stage_dir = Path(self._tmp.name) / "stage"
        self.stage_dir.mkdir()

    def tearDown(self):
        self._home_patch()
        self._tmp.cleanup()

    def _patch_home(self, fake_home: Path):
        return use_bottle_root(fake_home / ".bot-bottle")

    def test_prepare_leaves_the_queue_to_the_orchestrator(self):
        plan = prepare_supervise(_SUPERVISED, "dev")
        assert plan is not None
        # The queue lives in the orchestrator's registry.db, not the host DB.
        self.assertFalse(host_db_path().exists())
        self.assertEqual("dev", plan.slug)

    def test_prepare_skips_unsupervised_bottles(self):
        unsupervised = cast(ManifestBottle, SimpleNamespace(supervise=False))
        self.assertIsNone(prepare_supervise(unsupervised, "dev"))

    def test_prepare_does_not_create_current_config_dir(self):
        plan = prepare_supervise(_SUPERVISED, "dev")
        self.assertFalse((self.stage_dir / "current-config").exists())
        self.assertFalse(hasattr(plan, "current_config_dir"))


if __name__ == "__main__":
    unittest.main()
