"""Unit: supervise headless paths (PRD 0013 phase 4, PRD 0016).

The curses TUI itself isn't exercised here — these tests cover the
discovery + approve/reject paths that the TUI's key handlers call into.

egress-block (add_route) was removed in issue #198; the TestEgressApplyWiring
class and all stubs for add_route have been dropped accordingly.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bot_bottle import supervise
from bot_bottle.backend.docker.capability_apply import CapabilityApplyError
from bot_bottle.cli import supervise as supervise_cli
from bot_bottle.supervise import (
    Proposal,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_CAPABILITY_BLOCK,
    read_audit_entries,
    read_response,
    sha256_hex,
)


FIXED = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(slug: str = "dev", tool: str = TOOL_CAPABILITY_BLOCK) -> Proposal:
    payloads = {
        TOOL_CAPABILITY_BLOCK: "FROM python:3.13\n",
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
        self._original_apply_capability = supervise_cli.apply_capability_change
        supervise_cli.apply_capability_change = lambda slug, content: (  # type: ignore
            "FROM old\n", content,
        )

    def tearDown(self):
        supervise_cli.apply_capability_change = self._original_apply_capability
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


class TestCapabilityApplyWiring(_FakeHomeMixin, unittest.TestCase):
    """PRD 0016 Phase 3: approve() on a capability-block proposal
    calls apply_capability_change, archives the proposal afterward
    (sidecar is gone so it can't archive itself), and writes no
    audit entry (capability-block has none per PRD 0013)."""

    def setUp(self):
        self._setup_fake_home()
        self._original = supervise_cli.apply_capability_change

    def tearDown(self):
        supervise_cli.apply_capability_change = self._original
        self._teardown_fake_home()

    def _enqueue_capability(self, proposed: str = "FROM python:3.13\nRUN apk add ripgrep\n"):
        p = Proposal.new(
            bottle_slug="dev", tool=TOOL_CAPABILITY_BLOCK,
            proposed_file=proposed,
            justification="need ripgrep",
            current_file_hash=sha256_hex(proposed),
            now=FIXED,
        )
        qdir = supervise.queue_dir_for_slug("dev")
        qdir.mkdir(parents=True, exist_ok=True)
        supervise.write_proposal(qdir, p)
        return supervise_cli.QueuedProposal(proposal=p, queue_dir=qdir)

    def test_capability_block_calls_apply_with_proposed_file(self):
        calls = []
        supervise_cli.apply_capability_change = lambda slug, content: (  # type: ignore
            calls.append((slug, content)) or ("FROM old\n", content)
        )
        qp = self._enqueue_capability("FROM bookworm\n")
        supervise_cli.approve(qp)
        self.assertEqual([("dev", "FROM bookworm\n")], calls)

    def test_apply_failure_blocks_response_and_keeps_pending(self):
        supervise_cli.apply_capability_change = lambda slug, content: (_ for _ in ()).throw(  # type: ignore
            CapabilityApplyError("teardown failed")
        )
        qp = self._enqueue_capability()
        with self.assertRaises(CapabilityApplyError):
            supervise_cli.approve(qp)
        self.assertEqual(
            [qp.proposal.id],
            [p.id for p in supervise.list_pending_proposals(qp.queue_dir)],
        )

    def test_no_audit_log_for_capability(self):
        supervise_cli.apply_capability_change = lambda slug, content: ("FROM old\n", content)  # type: ignore
        qp = self._enqueue_capability()
        supervise_cli.approve(qp)
        self.assertEqual([], read_audit_entries("egress", "dev"))

    def test_proposal_archived_after_apply(self):
        supervise_cli.apply_capability_change = lambda slug, content: ("FROM old\n", content)  # type: ignore
        qp = self._enqueue_capability()
        supervise_cli.approve(qp)
        self.assertEqual([], supervise.list_pending_proposals(qp.queue_dir))
        processed = list((qp.queue_dir / "processed").glob("*.json"))
        self.assertEqual(2, len(processed))


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


class TestCapabilityBlockSmolmachinesGuard(_FakeHomeMixin, unittest.TestCase):
    """approve() must refuse capability-block for smolmachines bottles and
    pass it through for Docker bottles (PRD 0039)."""

    def setUp(self):
        self._setup_fake_home()
        self._original_apply_capability = supervise_cli.apply_capability_change
        supervise_cli.apply_capability_change = lambda slug, content: ("", content)  # type: ignore

    def tearDown(self):
        supervise_cli.apply_capability_change = self._original_apply_capability
        self._teardown_fake_home()

    def _enqueue_capability(self, slug: str = "dev") -> "supervise_cli.QueuedProposal":
        p = _proposal(slug=slug, tool=TOOL_CAPABILITY_BLOCK)
        qdir = supervise.queue_dir_for_slug(slug)
        qdir.mkdir(parents=True, exist_ok=True)
        supervise.write_proposal(qdir, p)
        return supervise_cli.QueuedProposal(proposal=p, queue_dir=qdir)

    def _write_metadata(self, slug: str, compose_project: str) -> None:
        from bot_bottle.backend.docker.bottle_state import BottleMetadata, write_metadata
        write_metadata(BottleMetadata(
            identity=slug,
            agent_name="myagent",
            cwd="",
            copy_cwd=False,
            started_at="2026-06-02T00:00:00+00:00",
            compose_project=compose_project,
        ))

    def test_smolmachines_bottle_raises_capability_apply_error(self):
        self._write_metadata("dev", compose_project="")
        qp = self._enqueue_capability("dev")
        with self.assertRaises(CapabilityApplyError) as ctx:
            supervise_cli.approve(qp)
        self.assertIn("smolmachines", str(ctx.exception))

    def test_docker_bottle_calls_apply_capability_change(self):
        self._write_metadata("dev", compose_project="bot-bottle-dev")
        qp = self._enqueue_capability("dev")
        supervise_cli.approve(qp)  # must not raise

    def test_no_metadata_falls_through_to_docker_path(self):
        qp = self._enqueue_capability("dev")
        supervise_cli.approve(qp)  # must not raise


if __name__ == "__main__":
    unittest.main()
