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
from bot_bottle import _supervise_types as _sv_types
from bot_bottle.audit_store import AuditStore
from bot_bottle.cli import supervise as supervise_cli
from bot_bottle.queue_store import QueueStore
from bot_bottle.supervise import (
    Proposal,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_EGRESS_ALLOW,
    TOOL_GITLEAKS_ALLOW,
    TOOL_EGRESS_TOKEN_ALLOW,
    read_audit_entries,
    read_response,
    sha256_hex,
)


FIXED = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(slug: str = "dev", tool: str = TOOL_EGRESS_ALLOW) -> Proposal:
    payloads = {
        supervise.TOOL_EGRESS_ALLOW: "routes:\n  - host: example.com\n",
        supervise.TOOL_EGRESS_BLOCK: "routes:\n  - host: example.com\n",
        TOOL_GITLEAKS_ALLOW: "file: tests/test_fixture.py\nline: 3\n",
        TOOL_EGRESS_TOKEN_ALLOW: "host: api.example.com\ndetector: token\n",
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
        original_sv = supervise.bot_bottle_root
        original_svt = _sv_types.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        _sv_types.bot_bottle_root = fake_root  # type: ignore[assignment]

        def restore() -> None:
            supervise.bot_bottle_root = original_sv  # type: ignore[assignment]
            _sv_types.bot_bottle_root = original_svt  # type: ignore[assignment]

        self._restore_home = restore
        QueueStore("").migrate()
        AuditStore().migrate()

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
            supervise.write_proposal(_proposal(slug=slug))
        pending = supervise_cli.discover_pending()
        self.assertEqual({"dev", "api"}, {qp.proposal.bottle_slug for qp in pending})

    def test_sorted_by_arrival_across_bottles(self):
        early = Proposal.new(
            bottle_slug="api", tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: early.example.com\n", justification="early",
            current_file_hash="h",
            now=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc),
        )
        late = Proposal.new(
            bottle_slug="dev", tool=TOOL_EGRESS_ALLOW,
            proposed_file="routes:\n  - host: late.example.com\n", justification="late",
            current_file_hash="h",
            now=datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc),
        )
        for p in (late, early):
            supervise.write_proposal(p)
        pending = supervise_cli.discover_pending()
        self.assertEqual([early.id, late.id], [qp.proposal.id for qp in pending])

    def test_excludes_already_responded(self):
        p = _proposal()
        supervise.write_proposal(p)
        supervise.write_response("dev", supervise.Response(
            proposal_id=p.id, status=STATUS_APPROVED, notes="",
        ))
        self.assertEqual([], supervise_cli.discover_pending())


class TestApproveReject(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def _enqueue(self, tool: str = TOOL_EGRESS_ALLOW):
        p = _proposal(tool=tool)
        supervise.write_proposal(p)
        return supervise_cli.QueuedProposal(proposal=p)

    def test_approve_writes_response(self):
        qp = self._enqueue()
        with patch(
            "bot_bottle.cli.supervise.apply_routes_change",
            return_value=("routes: []\n", "routes:\n  - host: example.com\n"),
        ):
            supervise_cli.approve(qp)
        resp = read_response(qp.proposal.bottle_slug, qp.proposal.id)
        self.assertEqual(STATUS_APPROVED, resp.status)
        self.assertIsNone(resp.final_file)

    def test_approve_with_final_file_marks_modified(self):
        qp = self._enqueue()
        with patch(
            "bot_bottle.cli.supervise.apply_routes_change",
            return_value=("routes: []\n", "routes:\n  - host: edited.example.com\n"),
        ):
            supervise_cli.approve(
                qp,
                final_file="routes:\n  - host: edited.example.com\n",
                notes="tweaked",
            )
        resp = read_response(qp.proposal.bottle_slug, qp.proposal.id)
        self.assertEqual(STATUS_MODIFIED, resp.status)
        self.assertEqual("routes:\n  - host: edited.example.com\n", resp.final_file)
        self.assertEqual("tweaked", resp.notes)

    def test_reject_writes_rejection(self):
        qp = self._enqueue()
        supervise_cli.reject(qp, reason="nope")
        resp = read_response(qp.proposal.bottle_slug, qp.proposal.id)
        self.assertEqual(STATUS_REJECTED, resp.status)
        self.assertEqual("nope", resp.notes)

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
        # Gate polls the DB for the response; TUI must not archive it.
        resp = read_response(qp.proposal.bottle_slug, qp.proposal.id)
        self.assertEqual(STATUS_APPROVED, resp.status)
        self.assertEqual("dummy fixture", resp.notes)

    def test_tui_gitleaks_allow_requires_reason(self):
        qp = self._enqueue(tool=TOOL_GITLEAKS_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value=""):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertEqual("approve aborted (empty reason)", status)

    def test_tui_gitleaks_allow_writes_reason(self):
        qp = self._enqueue(tool=TOOL_GITLEAKS_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value="test fixture"):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertIn("approved gitleaks-allow", status)
        resp = read_response(qp.proposal.bottle_slug, qp.proposal.id)
        self.assertEqual("test fixture", resp.notes)

    def test_approve_token_allow_leaves_response_for_egress(self):
        qp = self._enqueue(tool=TOOL_EGRESS_TOKEN_ALLOW)
        supervise_cli.approve(qp, notes="false positive")
        # The egress addon polls the DB for the response; the TUI must
        # not archive it (the addon archives after reading).
        resp = read_response(qp.proposal.bottle_slug, qp.proposal.id)
        self.assertEqual(STATUS_APPROVED, resp.status)
        self.assertEqual("false positive", resp.notes)

    def test_token_allow_writes_no_audit_log(self):
        qp = self._enqueue(tool=TOOL_EGRESS_TOKEN_ALLOW)
        supervise_cli.approve(qp, notes="false positive")
        self.assertEqual([], read_audit_entries("egress", "dev"))

    def test_tui_token_allow_requires_reason(self):
        qp = self._enqueue(tool=TOOL_EGRESS_TOKEN_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value=""):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertEqual("approve aborted (empty reason)", status)

    def test_tui_token_allow_writes_reason(self):
        qp = self._enqueue(tool=TOOL_EGRESS_TOKEN_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value="legit"):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertIn("approved egress-token-allow", status)
        resp = read_response(qp.proposal.bottle_slug, qp.proposal.id)
        self.assertEqual("legit", resp.notes)

    def test_suffix_for_token_allow_is_txt(self):
        self.assertEqual(".txt", supervise_cli._suffix_for_tool(TOOL_EGRESS_TOKEN_ALLOW))


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


if __name__ == "__main__":
    unittest.main()
