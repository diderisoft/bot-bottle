"""Unit: supervise headless paths (PRD 0013 phase 4, PRD 0016).

The curses TUI itself isn't exercised here — these tests cover the
discovery + approve/reject paths that the TUI's key handlers call into.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot_bottle import supervise
from bot_bottle.cli import supervise as supervise_cli
from bot_bottle.supervise import (
    Proposal,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_CAPABILITY_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    read_audit_entries,
    read_response,
    sha256_hex,
)


FIXED = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(slug: str = "dev", tool: str = TOOL_CAPABILITY_BLOCK) -> Proposal:
    payloads = {
        TOOL_CAPABILITY_BLOCK: "FROM python:3.13\n",
        supervise.TOOL_ALLOW: "routes:\n  - host: example.com\n",
        supervise.TOOL_EGRESS_BLOCK: "routes:\n  - host: example.com\n",
        TOOL_GITLEAKS_ALLOW: "file: tests/test_fixture.py\nline: 3\n",
    }
    payload = payloads.get(tool, "")
    return Proposal.new(
        bottle_slug=slug, tool=tool,
        proposed_file=payload,
        justification=f"needed for {slug}",
        current_file_hash=sha256_hex(payload),
        now=FIXED,
    )


class _FakeHomeMixin:
    """Patch supervise.bot_bottle_root to a temp dir for the test."""

    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-test.")
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        self._restore_home = lambda: setattr(supervise, "bot_bottle_root", original)

    def _teardown_fake_home(self):
        self._restore_home()
        self._tmp.cleanup()


class TestDiscoverPending(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_empty_when_no_queues(self):
        self.assertEqual([], supervise_cli.discover_pending())

    def test_walks_all_slug_subdirs(self):
        for slug in ("dev", "api"):
            qdir = supervise.queue_dir_for_slug(slug)
            qdir.mkdir(parents=True)
            supervise.write_proposal(qdir, _proposal(slug=slug))
        pending = supervise_cli.discover_pending()
        self.assertEqual({"dev", "api"}, {qp.proposal.bottle_slug for qp in pending})

    def test_sorted_by_arrival_across_bottles(self):
        early = Proposal.new(
            bottle_slug="api", tool=TOOL_CAPABILITY_BLOCK,
            proposed_file="FROM python:3.13\n", justification="early",
            current_file_hash="h",
            now=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc),
        )
        late = Proposal.new(
            bottle_slug="dev", tool=TOOL_CAPABILITY_BLOCK,
            proposed_file="FROM python:3.13\n", justification="late",
            current_file_hash="h",
            now=datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc),
        )
        for p in (late, early):
            qdir = supervise.queue_dir_for_slug(p.bottle_slug)
            qdir.mkdir(parents=True, exist_ok=True)
            supervise.write_proposal(qdir, p)
        pending = supervise_cli.discover_pending()
        self.assertEqual([early.id, late.id], [qp.proposal.id for qp in pending])

    def test_excludes_already_responded(self):
        p = _proposal()
        qdir = supervise.queue_dir_for_slug("dev")
        qdir.mkdir(parents=True)
        supervise.write_proposal(qdir, p)
        supervise.write_response(qdir, supervise.Response(
            proposal_id=p.id, status=STATUS_APPROVED, notes="",
        ))
        self.assertEqual([], supervise_cli.discover_pending())


class TestApproveReject(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def _enqueue(self, tool: str = TOOL_CAPABILITY_BLOCK):
        p = _proposal(tool=tool)
        qdir = supervise.queue_dir_for_slug("dev")
        qdir.mkdir(parents=True, exist_ok=True)
        supervise.write_proposal(qdir, p)
        return supervise_cli.QueuedProposal(proposal=p, queue_dir=qdir)

    def test_approve_writes_response(self):
        qp = self._enqueue()
        supervise_cli.approve(qp)
        # capability-block is archived on approve, so the response file
        # moves to processed/ before the caller can read it.
        resp = read_response(qp.queue_dir / "processed", qp.proposal.id)
        self.assertEqual(STATUS_APPROVED, resp.status)
        self.assertIsNone(resp.final_file)

    def test_approve_with_final_file_marks_modified(self):
        qp = self._enqueue()
        supervise_cli.approve(qp, final_file="FROM bookworm\n", notes="tweaked")
        resp = read_response(qp.queue_dir / "processed", qp.proposal.id)
        self.assertEqual(STATUS_MODIFIED, resp.status)
        self.assertEqual("FROM bookworm\n", resp.final_file)
        self.assertEqual("tweaked", resp.notes)

    def test_reject_writes_rejection(self):
        qp = self._enqueue()
        supervise_cli.reject(qp, reason="nope")
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_REJECTED, resp.status)
        self.assertEqual("nope", resp.notes)

    def test_no_audit_log_for_capability_block(self):
        qp = self._enqueue(tool=TOOL_CAPABILITY_BLOCK)
        supervise_cli.approve(qp)
        self.assertEqual([], read_audit_entries("egress", "dev"))

    def test_approve_egress_block_writes_audit_log(self):
        qp = self._enqueue(tool=supervise.TOOL_EGRESS_BLOCK)
        with patch(
            "bot_bottle.cli.supervise.apply_routes_change",
            return_value=("routes: []\n", "routes:\n  - host: example.com\n"),
        ) as apply_routes_change:
            supervise_cli.approve(qp)
        apply_routes_change.assert_called_once_with(
            "dev",
            "routes:\n  - host: example.com\n",
        )
        entries = read_audit_entries("egress", "dev")
        self.assertEqual(1, len(entries))
        self.assertEqual(STATUS_APPROVED, entries[0].operator_action)
        self.assertEqual("needed for dev", entries[0].justification)

    def test_approve_gitleaks_allow_leaves_response_for_gate(self):
        qp = self._enqueue(tool=TOOL_GITLEAKS_ALLOW)
        supervise_cli.approve(qp, notes="dummy fixture")
        # Gate polls the queue dir for the response; TUI must not archive it.
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_APPROVED, resp.status)
        self.assertEqual("dummy fixture", resp.notes)
        self.assertFalse((qp.queue_dir / "processed").exists())

    def test_tui_gitleaks_allow_requires_reason(self):
        qp = self._enqueue(tool=TOOL_GITLEAKS_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value=""):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertEqual("approve aborted (empty reason)", status)
        self.assertFalse((qp.queue_dir / "processed").exists())

    def test_tui_gitleaks_allow_writes_reason(self):
        qp = self._enqueue(tool=TOOL_GITLEAKS_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value="test fixture"):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertIn("approved gitleaks-allow", status)
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual("test fixture", resp.notes)


# class TestCapabilityApplyWiring(_FakeHomeMixin, unittest.TestCase):
#     # DISABLED — capability_apply functionality is currently commented out.
#     pass


class TestEditInEditor(unittest.TestCase):
    def test_runs_editor_returns_edited_content(self):
        original_editor = os.environ.get("EDITOR")
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="fake-editor.",
            ) as script:
                script.write('#!/bin/sh\nprintf "%s" "edited" > "$1"\n')
                editor_script = script.name
            os.chmod(editor_script, 0o755)
            os.environ["EDITOR"] = editor_script
            try:
                result = supervise_cli.edit_in_editor("original")
                self.assertEqual("edited", result)
            finally:
                os.unlink(editor_script)
        finally:
            if original_editor is None:
                os.environ.pop("EDITOR", None)
            else:
                os.environ["EDITOR"] = original_editor

    def test_returns_none_when_unchanged(self):
        original_editor = os.environ.get("EDITOR")
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="noop-editor.",
            ) as script:
                script.write('#!/bin/sh\n: $1\n')
                editor_script = script.name
            os.chmod(editor_script, 0o755)
            os.environ["EDITOR"] = editor_script
            try:
                result = supervise_cli.edit_in_editor("original")
                self.assertIsNone(result)
            finally:
                os.unlink(editor_script)
        finally:
            if original_editor is None:
                os.environ.pop("EDITOR", None)
            else:
                os.environ["EDITOR"] = original_editor


# class TestCapabilityBlockSmolmachinesGuard(_FakeHomeMixin, unittest.TestCase):
#     # DISABLED — capability_apply functionality is currently commented out.
#     pass


if __name__ == "__main__":
    unittest.main()
