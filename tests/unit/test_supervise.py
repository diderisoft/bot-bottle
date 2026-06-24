"""Unit: supervise queue + audit log + diff helpers (PRD 0013)."""

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bot_bottle import supervise
from bot_bottle.supervise import (
    AuditEntry,
    Proposal,
    Response,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_CAPABILITY_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    archive_proposal,
    audit_log_path,
    list_pending_proposals,
    read_audit_entries,
    read_proposal,
    read_response,
    render_diff,
    sha256_hex,
    wait_for_response,
    write_audit_entry,
    write_proposal,
    write_response,
)


FIXED_TS = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(
    tool: str = TOOL_CAPABILITY_BLOCK,
    proposed: str = "FROM python:3.13\n",
    justification: str = "need a capability",
) -> Proposal:
    return Proposal.new(
        bottle_slug="dev",
        tool=tool,
        proposed_file=proposed,
        justification=justification,
        current_file_hash=sha256_hex(proposed),
        now=FIXED_TS,
    )


class TestProposalRoundtrip(unittest.TestCase):
    def test_new_stamps_uuid_and_iso_timestamp(self):
        p = _proposal()
        self.assertTrue(p.id)
        self.assertEqual("2026-05-25T12:00:00+00:00", p.arrival_timestamp)
        self.assertEqual("dev", p.bottle_slug)
        self.assertEqual(TOOL_CAPABILITY_BLOCK, p.tool)

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
        self.queue_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_and_read_proposal(self):
        p = _proposal()
        path = write_proposal(self.queue_dir, p)
        self.assertTrue(path.exists())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        loaded = read_proposal(self.queue_dir, p.id)
        self.assertEqual(p, loaded)

    def test_list_pending_excludes_responded(self):
        a = _proposal(justification="first")
        b = _proposal(justification="second")
        write_proposal(self.queue_dir, a)
        write_proposal(self.queue_dir, b)
        write_response(self.queue_dir, Response(
            proposal_id=a.id, status=STATUS_APPROVED, notes="",
        ))
        pending = list_pending_proposals(self.queue_dir)
        self.assertEqual([b.id], [p.id for p in pending])

    def test_list_pending_returns_empty_for_missing_dir(self):
        self.assertEqual([], list_pending_proposals(self.queue_dir / "nope"))

    def test_list_pending_sorted_by_arrival(self):
        # Fabricate two with explicit timestamps.
        a = Proposal.new(
            bottle_slug="dev", tool=TOOL_CAPABILITY_BLOCK,
            proposed_file="FROM python:3.13\n", justification="early",
            current_file_hash="x",
            now=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc),
        )
        b = Proposal.new(
            bottle_slug="dev", tool=TOOL_CAPABILITY_BLOCK,
            proposed_file="FROM python:3.13\n", justification="late",
            current_file_hash="x",
            now=datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc),
        )
        # Write in reverse order.
        write_proposal(self.queue_dir, b)
        write_proposal(self.queue_dir, a)
        ordered = list_pending_proposals(self.queue_dir)
        self.assertEqual([a.id, b.id], [p.id for p in ordered])

    def test_write_and_read_response(self):
        r = Response(proposal_id="xyz", status=STATUS_REJECTED, notes="no")
        write_response(self.queue_dir, r)
        self.assertEqual(r, read_response(self.queue_dir, "xyz"))

    def test_wait_for_response_returns_when_file_appears(self):
        p = _proposal()
        write_proposal(self.queue_dir, p)

        def write_after_delay():
            time.sleep(0.05)
            write_response(self.queue_dir, Response(
                proposal_id=p.id, status=STATUS_APPROVED, notes="ok",
            ))

        t = threading.Thread(target=write_after_delay)
        t.start()
        try:
            r = wait_for_response(self.queue_dir, p.id, poll_interval=0.01)
        finally:
            t.join()
        self.assertEqual(STATUS_APPROVED, r.status)
        self.assertEqual("ok", r.notes)

    def test_wait_for_response_times_out(self):
        deadline = time.monotonic() + 0.05
        with self.assertRaises(TimeoutError):
            wait_for_response(
                self.queue_dir, "never",
                poll_interval=0.01, deadline=deadline,
            )

    def test_archive_proposal_moves_both_files(self):
        p = _proposal()
        write_proposal(self.queue_dir, p)
        write_response(self.queue_dir, Response(
            proposal_id=p.id, status=STATUS_APPROVED, notes="",
        ))
        archive_proposal(self.queue_dir, p.id)
        self.assertFalse((self.queue_dir / f"{p.id}.proposal.json").exists())
        self.assertFalse((self.queue_dir / f"{p.id}.response.json").exists())
        self.assertTrue((self.queue_dir / "processed" / f"{p.id}.proposal.json").exists())
        self.assertTrue((self.queue_dir / "processed" / f"{p.id}.response.json").exists())

    def test_archive_is_idempotent_on_missing_files(self):
        # Should not raise.
        archive_proposal(self.queue_dir, "nope")


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-supervise-audit.")
        self._home_patch = self._patch_home(Path(self._tmp.name))

    def tearDown(self):
        self._home_patch()
        self._tmp.cleanup()

    def _patch_home(self, fake_home: Path):
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return fake_home / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        return lambda: setattr(supervise, "bot_bottle_root", original)

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
        path = write_audit_entry(e)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        loaded = read_audit_entries("cred-proxy", "dev")
        self.assertEqual([e], loaded)

    def test_appends_one_line_per_entry(self):
        for i in range(3):
            write_audit_entry(AuditEntry(
                timestamp=f"2026-05-25T12:00:0{i}+00:00",
                bottle_slug="dev",
                component="egress",
                operator_action=STATUS_APPROVED,
                operator_notes=f"n{i}",
                justification="",
                diff="",
            ))
        path = audit_log_path("egress", "dev")
        with path.open() as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(3, len(lines))
        for line in lines:
            self.assertTrue(json.loads(line))  # each line is valid JSON

    def test_separate_logs_per_component_slug(self):
        write_audit_entry(AuditEntry(
            timestamp="t",
            bottle_slug="dev",
            component="cred-proxy",
            operator_action=STATUS_APPROVED,
            operator_notes="",
            justification="",
            diff="",
        ))
        write_audit_entry(AuditEntry(
            timestamp="t",
            bottle_slug="dev",
            component="egress",
            operator_action=STATUS_APPROVED,
            operator_notes="",
            justification="",
            diff="",
        ))
        write_audit_entry(AuditEntry(
            timestamp="t",
            bottle_slug="other",
            component="cred-proxy",
            operator_action=STATUS_REJECTED,
            operator_notes="",
            justification="",
            diff="",
        ))
        self.assertEqual(1, len(read_audit_entries("cred-proxy", "dev")))
        self.assertEqual(1, len(read_audit_entries("egress", "dev")))
        self.assertEqual(1, len(read_audit_entries("cred-proxy", "other")))

    def test_read_audit_entries_missing_log_returns_empty(self):
        self.assertEqual([], read_audit_entries("cred-proxy", "no-such-bottle"))


class TestDiffAndHash(unittest.TestCase):
    def test_render_diff_returns_empty_when_unchanged(self):
        self.assertEqual("", render_diff("a\nb\n", "a\nb\n"))

    def test_render_diff_shows_changes(self):
        diff = render_diff("a\nb\nc\n", "a\nB\nc\n", label="routes.yaml")
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
                supervise.TOOL_ALLOW,
                TOOL_CAPABILITY_BLOCK,
                supervise.TOOL_EGRESS_BLOCK,
                TOOL_GITLEAKS_ALLOW,
                supervise.TOOL_EGRESS_TOKEN_ALLOW,
                supervise.TOOL_LIST_EGRESS_ROUTES,
            ),
            supervise.TOOLS,
        )

    def test_token_allow_proposal_roundtrips(self):
        p = Proposal.new(
            bottle_slug="dev",
            tool=supervise.TOOL_EGRESS_TOKEN_ALLOW,
            proposed_file="host: api.example.com\n",
            justification="false positive",
            current_file_hash="h",
        )
        self.assertEqual(p, Proposal.from_dict(p.to_dict()))

    def test_component_map_has_egress_entries(self):
        self.assertEqual(
            {
                supervise.TOOL_ALLOW: "egress",
                supervise.TOOL_EGRESS_BLOCK: "egress",
            },
            supervise.COMPONENT_FOR_TOOL,
        )


class _StubSupervise(supervise.Supervise):
    """Concrete Supervise subclass for testing the prepare template."""

    def start(self, plan):  # type: ignore
        return f"stub-{plan.slug}"

    def stop(self, target):  # type: ignore
        return None


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
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return fake_home / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        return lambda: setattr(supervise, "bot_bottle_root", original)

    def test_prepare_creates_queue_and_current_config(self):
        plan = _StubSupervise().prepare("dev", self.stage_dir)
        self.assertTrue(plan.queue_dir.is_dir())
        self.assertTrue(plan.current_config_dir.is_dir())
        self.assertEqual("dev", plan.slug)
        self.assertEqual("", plan.internal_network)

    def test_prepare_writes_no_files_to_current_config(self):
        # dockerfile_content is no longer accepted by prepare.
        # routes.yaml + allowlist live behind the
        # `list-egress-routes` MCP tool (PRD 0017 chunk 3).
        plan = _StubSupervise().prepare("dev", self.stage_dir)
        files = sorted(p.name for p in plan.current_config_dir.iterdir())
        self.assertEqual([], files)


if __name__ == "__main__":
    unittest.main()
