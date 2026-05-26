"""Unit: dashboard headless paths (PRD 0013 phase 4, PRD 0014).

The curses TUI itself isn't exercised here — these tests cover the
discovery + approve/reject + audit-write paths that the TUI's key
handlers call into.

apply_routes_change is stubbed at the dashboard module level so the
tests don't need a running cred-proxy sidecar; the real docker
exec/cp/SIGHUP plumbing is covered by the integration test.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.backend.docker.capability_apply import CapabilityApplyError
from claude_bottle.backend.docker.egress_apply import EgressApplyError
from claude_bottle.backend.docker.pipelock_apply import PipelockApplyError
from claude_bottle.cli import dashboard
from claude_bottle.supervise import (
    Proposal,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_CAPABILITY_BLOCK,
    TOOL_EGRESS_BLOCK,
    TOOL_PIPELOCK_BLOCK,
    read_audit_entries,
    read_response,
    sha256_hex,
)


FIXED = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(slug: str = "dev", tool: str = TOOL_EGRESS_BLOCK) -> Proposal:
    # Per-tool payload shape: cred-proxy gets routes.yaml, pipelock
    # gets a failed URL (PR #25 follow-up), capability gets a
    # Dockerfile-ish blob. Match the production dispatch in
    # PROPOSED_FILE_FIELD.
    payloads = {
        TOOL_EGRESS_BLOCK: '{"routes": []}\n',
        TOOL_PIPELOCK_BLOCK: "https://example.com/path",
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
            bottle_slug="api", tool=TOOL_EGRESS_BLOCK,
            proposed_file="{}", justification="early",
            current_file_hash="h",
            now=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc),
        )
        late = Proposal.new(
            bottle_slug="dev", tool=TOOL_EGRESS_BLOCK,
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
        self._original_add_route = dashboard.add_route
        self._original_apply_allowlist = dashboard.apply_allowlist_change
        self._original_fetch_allowlist = dashboard.fetch_current_allowlist
        self._original_apply_capability = dashboard.apply_capability_change
        # Default stubs: succeed with deterministic before/after so the
        # audit log shows a non-empty diff.
        dashboard.add_route = lambda slug, content: (
            '{"routes": []}\n', '{"routes": [{"host": "x"}]}\n',
        )
        dashboard.apply_allowlist_change = lambda slug, content: (
            "old.example\n", content,
        )
        dashboard.fetch_current_allowlist = lambda slug: "old.example\n"
        dashboard.apply_capability_change = lambda slug, content: (
            "FROM old\n", content,
        )

    def tearDown(self):
        dashboard.add_route = self._original_add_route
        dashboard.apply_allowlist_change = self._original_apply_allowlist
        dashboard.fetch_current_allowlist = self._original_fetch_allowlist
        dashboard.apply_capability_change = self._original_apply_capability
        self._teardown_fake_home()

    def _enqueue(self, tool: str = TOOL_EGRESS_BLOCK):
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
        entries = read_audit_entries("egress", "dev")
        self.assertEqual(1, len(entries))
        self.assertEqual("approved", entries[0].operator_action)

    def test_approve_with_final_file_marks_modified(self):
        qp = self._enqueue()
        dashboard.approve(qp, final_file='{"routes": [{"path": "/x/"}]}\n', notes="tweaked")
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_MODIFIED, resp.status)
        self.assertEqual('{"routes": [{"path": "/x/"}]}\n', resp.final_file)
        self.assertEqual("tweaked", resp.notes)
        entries = read_audit_entries("egress", "dev")
        self.assertEqual("modified", entries[0].operator_action)

    def test_reject_writes_rejection(self):
        qp = self._enqueue()
        dashboard.reject(qp, reason="nope")
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_REJECTED, resp.status)
        self.assertEqual("nope", resp.notes)
        entries = read_audit_entries("egress", "dev")
        self.assertEqual("rejected", entries[0].operator_action)
        self.assertEqual("nope", entries[0].operator_notes)

    def test_capability_block_skips_audit_log(self):
        qp = self._enqueue(tool=TOOL_CAPABILITY_BLOCK)
        dashboard.approve(qp)
        # No audit log for capability-block (per PRD 0013 / 0016).
        # cred-proxy and pipelock logs both empty.
        self.assertEqual([], read_audit_entries("egress", "dev"))
        self.assertEqual([], read_audit_entries("pipelock", "dev"))

    def test_pipelock_audit_distinct_from_egress(self):
        qp = self._enqueue(tool=TOOL_PIPELOCK_BLOCK)
        dashboard.approve(qp)
        self.assertEqual(1, len(read_audit_entries("pipelock", "dev")))
        self.assertEqual(0, len(read_audit_entries("egress", "dev")))


class TestEgressApplyWiring(_FakeHomeMixin, unittest.TestCase):
    """PRD 0017 chunk 3: approve() on an egress-block proposal
    must call add_route (single-route merge) with the right args
    and surface its failures."""

    def setUp(self):
        self._setup_fake_home()
        self._original_add_route = dashboard.add_route

    def tearDown(self):
        dashboard.add_route = self._original_add_route
        self._teardown_fake_home()

    def _enqueue_egress(self, proposed: str = '{"host": "x.example"}\n'):
        p = Proposal.new(
            bottle_slug="dev", tool=TOOL_EGRESS_BLOCK,
            proposed_file=proposed,
            justification="need a route",
            current_file_hash=sha256_hex(proposed),
            now=FIXED,
        )
        qdir = supervise.queue_dir_for_slug("dev")
        qdir.mkdir(parents=True, exist_ok=True)
        supervise.write_proposal(qdir, p)
        return dashboard.QueuedProposal(proposal=p, queue_dir=qdir)

    def test_egress_block_calls_add_route_with_proposed_json(self):
        calls = []
        dashboard.add_route = lambda slug, content: (
            calls.append((slug, content)) or ("before", "after")
        )
        qp = self._enqueue_egress(
            proposed='{"host": "new.example", "path_allowlist": ["/x/"]}\n'
        )
        dashboard.approve(qp)
        self.assertEqual(1, len(calls))
        slug, content = calls[0]
        self.assertEqual("dev", slug)
        # The single-route JSON the agent proposed reaches add_route
        # unchanged — add_route fetches current state + merges.
        self.assertEqual(
            '{"host": "new.example", "path_allowlist": ["/x/"]}\n',
            content,
        )

    def test_modify_passes_final_file_to_add_route(self):
        calls = []
        dashboard.add_route = lambda slug, content: (
            calls.append(content) or ("before", "after")
        )
        qp = self._enqueue_egress()
        dashboard.approve(
            qp,
            final_file='{"host": "edited.example"}\n',
            notes="tweaked",
        )
        self.assertEqual(['{"host": "edited.example"}\n'], calls)

    def test_apply_failure_blocks_response_and_audit(self):
        dashboard.add_route = lambda slug, content: (_ for _ in ()).throw(
            EgressApplyError("docker exec failed")
        )
        qp = self._enqueue_egress()
        with self.assertRaises(EgressApplyError):
            dashboard.approve(qp)
        # No response file (proposal stays pending).
        self.assertEqual(
            [qp.proposal.id],
            [p.id for p in supervise.list_pending_proposals(qp.queue_dir)],
        )
        # No audit entry.
        self.assertEqual([], read_audit_entries("egress", "dev"))

    def test_real_diff_lands_in_audit(self):
        dashboard.add_route = lambda slug, content: (
            '{"routes": []}\n',  # before
            '{"routes": [{"host": "new.example"}]}\n',  # after
        )
        qp = self._enqueue_egress(proposed='{"host": "new.example"}\n')
        dashboard.approve(qp)
        entries = read_audit_entries("egress", "dev")
        self.assertEqual(1, len(entries))
        self.assertIn('+{"routes": [{"host": "new.example"}]}', entries[0].diff)
        self.assertIn('-{"routes": []}', entries[0].diff)

    def test_reject_does_not_call_apply(self):
        called = []
        dashboard.apply_routes_change = lambda slug, content: (
            called.append(True) or ("", content)
        )
        qp = self._enqueue_egress()
        dashboard.reject(qp, reason="no thanks")
        self.assertEqual([], called)
        # Reject still writes a response + audit entry with empty diff.
        resp = read_response(qp.queue_dir, qp.proposal.id)
        self.assertEqual(STATUS_REJECTED, resp.status)
        entries = read_audit_entries("egress", "dev")
        self.assertEqual(1, len(entries))
        self.assertEqual("", entries[0].diff)


class TestPipelockApplyWiring(_FakeHomeMixin, unittest.TestCase):
    """PRD 0015 Phase 2 + PR #25 follow-up: approve() on a
    pipelock-block proposal carries the failed URL; the dashboard
    extracts the host, merges it into the running allowlist, and
    calls apply_allowlist_change with the merged content."""

    def setUp(self):
        self._setup_fake_home()
        self._original_apply = dashboard.apply_allowlist_change
        self._original_fetch = dashboard.fetch_current_allowlist

    def tearDown(self):
        dashboard.apply_allowlist_change = self._original_apply
        dashboard.fetch_current_allowlist = self._original_fetch
        self._teardown_fake_home()

    def _enqueue_pipelock(self, failed_url: str = "https://api.github.com/repos/foo/bar"):
        p = Proposal.new(
            bottle_slug="dev", tool=TOOL_PIPELOCK_BLOCK,
            proposed_file=failed_url,
            justification="need to read PR metadata",
            current_file_hash=sha256_hex(failed_url),
            now=FIXED,
        )
        qdir = supervise.queue_dir_for_slug("dev")
        qdir.mkdir(parents=True, exist_ok=True)
        supervise.write_proposal(qdir, p)
        return dashboard.QueuedProposal(proposal=p, queue_dir=qdir)

    def test_url_host_merged_into_current_allowlist(self):
        dashboard.fetch_current_allowlist = lambda slug: "existing.example\n"
        applied = []
        dashboard.apply_allowlist_change = lambda slug, content: (
            applied.append((slug, content))
            or ("existing.example\n", content)
        )
        qp = self._enqueue_pipelock("https://api.github.com/repos/foo/bar")
        dashboard.approve(qp)
        # apply_allowlist_change was called with the merged content:
        # existing host + the URL's host (no path, since pipelock is
        # hostname-only).
        self.assertEqual(1, len(applied))
        slug, content = applied[0]
        self.assertEqual("dev", slug)
        self.assertIn("existing.example", content)
        self.assertIn("api.github.com", content)
        self.assertNotIn("/repos/foo/bar", content)  # path stripped

    def test_host_already_in_allowlist_is_idempotent(self):
        dashboard.fetch_current_allowlist = lambda slug: "api.github.com\n"
        applied = []
        dashboard.apply_allowlist_change = lambda slug, content: (
            applied.append(content)
            or ("api.github.com\n", content)
        )
        qp = self._enqueue_pipelock("https://api.github.com/some/path")
        dashboard.approve(qp)
        # Still applied, but the content is unchanged from current —
        # before/after diff is empty.
        self.assertEqual(1, len(applied))
        self.assertEqual("api.github.com\n", applied[0])

    def test_apply_failure_blocks_response_and_audit(self):
        dashboard.fetch_current_allowlist = lambda slug: "existing.example\n"
        dashboard.apply_allowlist_change = lambda slug, content: (_ for _ in ()).throw(
            PipelockApplyError("docker exec failed")
        )
        qp = self._enqueue_pipelock()
        with self.assertRaises(PipelockApplyError):
            dashboard.approve(qp)
        self.assertEqual(
            [qp.proposal.id],
            [p.id for p in supervise.list_pending_proposals(qp.queue_dir)],
        )
        self.assertEqual([], read_audit_entries("pipelock", "dev"))

    def test_url_without_host_raises(self):
        dashboard.fetch_current_allowlist = lambda slug: ""
        # supervise_server's validator would catch this; if a broken
        # URL ever makes it through, the dashboard surfaces it too.
        qp = self._enqueue_pipelock("https:///nohost")
        with self.assertRaises(PipelockApplyError):
            dashboard.approve(qp)


class TestCapabilityApplyWiring(_FakeHomeMixin, unittest.TestCase):
    """PRD 0016 Phase 3: approve() on a capability-block proposal
    calls apply_capability_change, archives the proposal afterward
    (sidecar is gone so it can't archive itself), and writes no
    audit entry (capability-block has none per PRD 0013)."""

    def setUp(self):
        self._setup_fake_home()
        self._original = dashboard.apply_capability_change

    def tearDown(self):
        dashboard.apply_capability_change = self._original
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
        return dashboard.QueuedProposal(proposal=p, queue_dir=qdir)

    def test_capability_block_calls_apply_with_proposed_file(self):
        calls = []
        dashboard.apply_capability_change = lambda slug, content: (
            calls.append((slug, content)) or ("FROM old\n", content)
        )
        qp = self._enqueue_capability("FROM bookworm\n")
        dashboard.approve(qp)
        self.assertEqual([("dev", "FROM bookworm\n")], calls)

    def test_apply_failure_blocks_response_and_keeps_pending(self):
        dashboard.apply_capability_change = lambda slug, content: (_ for _ in ()).throw(
            CapabilityApplyError("teardown failed")
        )
        qp = self._enqueue_capability()
        with self.assertRaises(CapabilityApplyError):
            dashboard.approve(qp)
        self.assertEqual(
            [qp.proposal.id],
            [p.id for p in supervise.list_pending_proposals(qp.queue_dir)],
        )

    def test_no_audit_log_for_capability(self):
        dashboard.apply_capability_change = lambda slug, content: ("FROM old\n", content)
        qp = self._enqueue_capability()
        dashboard.approve(qp)
        # capability-block has no audit log per PRD 0013 — its record
        # lives in the per-bottle Dockerfile + transcript state.
        self.assertEqual([], read_audit_entries("egress", "dev"))
        self.assertEqual([], read_audit_entries("pipelock", "dev"))

    def test_proposal_archived_after_apply(self):
        dashboard.apply_capability_change = lambda slug, content: ("FROM old\n", content)
        qp = self._enqueue_capability()
        dashboard.approve(qp)
        # Sidecar would normally archive after delivering the response,
        # but it's gone by then. The dashboard archives so
        # discover_pending stops surfacing the resolved proposal.
        self.assertEqual([], supervise.list_pending_proposals(qp.queue_dir))
        processed = list((qp.queue_dir / "processed").glob("*.json"))
        self.assertEqual(2, len(processed))


class TestOperatorEditRoutes(_FakeHomeMixin, unittest.TestCase):
    """PRD 0014 Phase 4: operator-initiated routes edit (not gated
    on a pending proposal)."""

    def setUp(self):
        self._setup_fake_home()
        self._original_apply = dashboard.apply_routes_change

    def tearDown(self):
        dashboard.apply_routes_change = self._original_apply
        self._teardown_fake_home()

    def test_writes_audit_with_operator_edit_action(self):
        dashboard.apply_routes_change = lambda slug, content: (
            '{"routes": []}\n', content,
        )
        dashboard.operator_edit_routes("dev", '{"routes": [{"path": "/x/"}]}\n')
        entries = read_audit_entries("egress", "dev")
        self.assertEqual(1, len(entries))
        self.assertEqual(supervise.ACTION_OPERATOR_EDIT, entries[0].operator_action)
        self.assertEqual("", entries[0].justification)
        self.assertIn("+", entries[0].diff)

    def test_failure_does_not_write_audit(self):
        dashboard.apply_routes_change = lambda slug, content: (_ for _ in ()).throw(
            EgressApplyError("nope")
        )
        with self.assertRaises(EgressApplyError):
            dashboard.operator_edit_routes("dev", '{"routes": []}\n')
        self.assertEqual([], read_audit_entries("egress", "dev"))


class TestOperatorEditAllowlist(_FakeHomeMixin, unittest.TestCase):
    """PRD 0015 Phase 3: operator-initiated pipelock allowlist edit."""

    def setUp(self):
        self._setup_fake_home()
        self._original = dashboard.apply_allowlist_change

    def tearDown(self):
        dashboard.apply_allowlist_change = self._original
        self._teardown_fake_home()

    def test_writes_audit_with_operator_edit_action(self):
        dashboard.apply_allowlist_change = lambda slug, content: (
            "old.example\n", content,
        )
        dashboard.operator_edit_allowlist("dev", "old.example\nnew.example\n")
        entries = read_audit_entries("pipelock", "dev")
        self.assertEqual(1, len(entries))
        self.assertEqual(supervise.ACTION_OPERATOR_EDIT, entries[0].operator_action)
        self.assertIn("+new.example", entries[0].diff)

    def test_failure_does_not_write_audit(self):
        dashboard.apply_allowlist_change = lambda slug, content: (_ for _ in ()).throw(
            PipelockApplyError("nope")
        )
        with self.assertRaises(PipelockApplyError):
            dashboard.operator_edit_allowlist("dev", "x.example\n")
        self.assertEqual([], read_audit_entries("pipelock", "dev"))


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
