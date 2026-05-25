"""Unit: dashboard headless paths (PRD 0013 phase 4).

The curses TUI itself isn't exercised here — these tests cover the
discovery + approve/reject + audit-write paths that the TUI's key
handlers call into.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.cli import dashboard
from claude_bottle.supervise import (
    Proposal,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_CAPABILITY_BLOCK,
    TOOL_CRED_PROXY_BLOCK,
    TOOL_PIPELOCK_BLOCK,
    read_audit_entries,
    read_response,
    sha256_hex,
)


FIXED = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(slug: str = "dev", tool: str = TOOL_CRED_PROXY_BLOCK) -> Proposal:
    return Proposal.new(
        bottle_slug=slug, tool=tool,
        proposed_file='{"routes": []}\n',
        justification=f"needed for {slug}",
        current_file_hash=sha256_hex("{}"),
        now=FIXED,
    )


class _FakeHomeMixin:
    """Patch supervise.claude_bottle_root to a temp dir for the test."""

    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dashboard-test.")
        original = supervise.claude_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".claude-bottle"

        supervise.claude_bottle_root = fake_root  # type: ignore[assignment]
        self._restore_home = lambda: setattr(supervise, "claude_bottle_root", original)

    def _teardown_fake_home(self):
        self._restore_home()
        self._tmp.cleanup()


class TestDiscoverPending(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_empty_when_no_queues(self):
        self.assertEqual([], dashboard.discover_pending())

    def test_walks_all_slug_subdirs(self):
        for slug in ("dev", "api"):
            qdir = supervise.queue_dir_for_slug(slug)
            qdir.mkdir(parents=True)
            supervise.write_proposal(qdir, _proposal(slug=slug))
        pending = dashboard.discover_pending()
        self.assertEqual({"dev", "api"}, {qp.proposal.bottle_slug for qp in pending})

    def test_sorted_by_arrival_across_bottles(self):
        early = Proposal.new(
            bottle_slug="api", tool=TOOL_CRED_PROXY_BLOCK,
            proposed_file="{}", justification="early",
            current_file_hash="h",
            now=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc),
        )
        late = Proposal.new(
            bottle_slug="dev", tool=TOOL_CRED_PROXY_BLOCK,
            proposed_file="{}", justification="late",
            current_file_hash="h",
            now=datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc),
        )
        for p in (late, early):
            qdir = supervise.queue_dir_for_slug(p.bottle_slug)
            qdir.mkdir(parents=True, exist_ok=True)
            supervise.write_proposal(qdir, p)
        pending = dashboard.discover_pending()
        self.assertEqual([early.id, late.id], [qp.proposal.id for qp in pending])

    def test_excludes_already_responded(self):
        p = _proposal()
        qdir = supervise.queue_dir_for_slug("dev")
        qdir.mkdir(parents=True)
        supervise.write_proposal(qdir, p)
        supervise.write_response(qdir, supervise.Response(
            proposal_id=p.id, status=STATUS_APPROVED, notes="",
        ))
        self.assertEqual([], dashboard.discover_pending())


class TestApproveReject(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def _enqueue(self, tool: str = TOOL_CRED_PROXY_BLOCK):
        p = _proposal(tool=tool)
        qdir = supervise.queue_dir_for_slug("dev")
        qdir.mkdir(parents=True, exist_ok=True)
        supervise.write_proposal(qdir, p)
        return dashboard.QueuedProposal(proposal=p, queue_dir=qdir)

    def test_approve_writes_response_and_audit(self):
        qp = self._enqueue()
        dashboard.approve(qp)
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_APPROVED, resp.status)
        self.assertIsNone(resp.final_file)
        entries = read_audit_entries("cred-proxy", "dev")
        self.assertEqual(1, len(entries))
        self.assertEqual("approved", entries[0].operator_action)

    def test_approve_with_final_file_marks_modified(self):
        qp = self._enqueue()
        dashboard.approve(qp, final_file='{"routes": [{"path": "/x/"}]}\n', notes="tweaked")
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_MODIFIED, resp.status)
        self.assertEqual('{"routes": [{"path": "/x/"}]}\n', resp.final_file)
        self.assertEqual("tweaked", resp.notes)
        entries = read_audit_entries("cred-proxy", "dev")
        self.assertEqual("modified", entries[0].operator_action)

    def test_reject_writes_rejection(self):
        qp = self._enqueue()
        dashboard.reject(qp, reason="nope")
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_REJECTED, resp.status)
        self.assertEqual("nope", resp.notes)
        entries = read_audit_entries("cred-proxy", "dev")
        self.assertEqual("rejected", entries[0].operator_action)
        self.assertEqual("nope", entries[0].operator_notes)

    def test_capability_block_skips_audit_log(self):
        qp = self._enqueue(tool=TOOL_CAPABILITY_BLOCK)
        dashboard.approve(qp)
        # No audit log for capability-block (per PRD 0013 / 0016).
        # cred-proxy and pipelock logs both empty.
        self.assertEqual([], read_audit_entries("cred-proxy", "dev"))
        self.assertEqual([], read_audit_entries("pipelock", "dev"))

    def test_pipelock_audit_distinct_from_cred_proxy(self):
        qp = self._enqueue(tool=TOOL_PIPELOCK_BLOCK)
        dashboard.approve(qp)
        self.assertEqual(1, len(read_audit_entries("pipelock", "dev")))
        self.assertEqual(0, len(read_audit_entries("cred-proxy", "dev")))


class TestEditInEditor(unittest.TestCase):
    def test_runs_editor_returns_edited_content(self):
        # Fake "editor" is /bin/sh -c 'cat <<EOF > $1 ... EOF'
        original_editor = os.environ.get("EDITOR")
        try:
            # Use a fake editor that overwrites the file with a known
            # marker. EDITOR is split with shlex equivalence by
            # subprocess.run when invoked as a list — keep it as a
            # single program path that takes the file as argv[1].
            os.environ["EDITOR"] = (
                "/bin/sh -c 'printf %s \"edited\" > \"$0\"'"
            )
            # subprocess.run with the str as the first list element
            # would try to find a binary literally named "/bin/sh -c ..."
            # — that won't work. Use shell mode trick: wrap in a script.
            # Easier: build a tiny helper script.
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="fake-editor.",
            ) as script:
                script.write('#!/bin/sh\nprintf "%s" "edited" > "$1"\n')
                editor_script = script.name
            os.chmod(editor_script, 0o755)
            os.environ["EDITOR"] = editor_script
            try:
                result = dashboard.edit_in_editor("original")
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
            # No-op editor: touch the file (leaves it unchanged).
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="noop-editor.",
            ) as script:
                script.write('#!/bin/sh\n: $1\n')
                editor_script = script.name
            os.chmod(editor_script, 0o755)
            os.environ["EDITOR"] = editor_script
            try:
                result = dashboard.edit_in_editor("original")
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
