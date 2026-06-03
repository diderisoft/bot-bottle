"""Unit: dashboard_model — state/model layer extracted from dashboard.py.

Tests for functions that were previously buried in the 2103-line
dashboard.py and had no coverage: _approval_status,
_proposed_payload_label, and _suffix_for_tool."""

import unittest
from pathlib import Path

from bot_bottle.cli.dashboard_model import (
    QueuedProposal,
    _approval_status,
    _proposed_payload_label,
    _suffix_for_tool,
)
from bot_bottle.supervise import (
    Proposal,
    TOOL_CAPABILITY_BLOCK,
    TOOL_EGRESS_BLOCK,
    TOOL_PIPELOCK_BLOCK,
    sha256_hex,
)
from datetime import datetime, timezone


def _qp(tool: str, slug: str = "dev") -> QueuedProposal:
    payload = "x"
    p = Proposal.new(
        bottle_slug=slug,
        tool=tool,
        proposed_file=payload,
        justification="test",
        current_file_hash=sha256_hex(payload),
        now=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    return QueuedProposal(proposal=p, queue_dir=Path("/tmp/q"))


class TestApprovalStatus(unittest.TestCase):
    def test_egress_block_base_message(self):
        qp = _qp(TOOL_EGRESS_BLOCK, slug="my-bot")
        msg = _approval_status(qp, "approved")
        self.assertEqual("approved egress-block for [my-bot]", msg)

    def test_modified_verb(self):
        qp = _qp(TOOL_PIPELOCK_BLOCK, slug="dev")
        msg = _approval_status(qp, "modified+approved")
        self.assertEqual("modified+approved pipelock-block for [dev]", msg)

    def test_capability_block_appends_resume_hint(self):
        qp = _qp(TOOL_CAPABILITY_BLOCK, slug="alpha")
        msg = _approval_status(qp, "approved")
        self.assertIn("resume: ./cli.py resume alpha", msg)
        self.assertIn("approved capability-block for [alpha]", msg)

    def test_egress_block_has_no_resume_hint(self):
        qp = _qp(TOOL_EGRESS_BLOCK)
        self.assertNotIn("resume", _approval_status(qp, "approved"))

    def test_pipelock_block_has_no_resume_hint(self):
        qp = _qp(TOOL_PIPELOCK_BLOCK)
        self.assertNotIn("resume", _approval_status(qp, "approved"))


class TestProposedPayloadLabel(unittest.TestCase):
    def test_pipelock_returns_failed_url(self):
        self.assertEqual("failed URL", _proposed_payload_label(TOOL_PIPELOCK_BLOCK))

    def test_egress_returns_proposed_file(self):
        self.assertEqual("proposed file", _proposed_payload_label(TOOL_EGRESS_BLOCK))

    def test_capability_returns_proposed_file(self):
        self.assertEqual("proposed file", _proposed_payload_label(TOOL_CAPABILITY_BLOCK))

    def test_unknown_tool_returns_proposed_file(self):
        self.assertEqual("proposed file", _proposed_payload_label("unknown-tool"))


class TestSuffixForTool(unittest.TestCase):
    def test_capability_block_returns_dockerfile_suffix(self):
        self.assertEqual(".dockerfile", _suffix_for_tool(TOOL_CAPABILITY_BLOCK))

    def test_egress_block_returns_txt(self):
        self.assertEqual(".txt", _suffix_for_tool(TOOL_EGRESS_BLOCK))

    def test_pipelock_block_returns_txt(self):
        self.assertEqual(".txt", _suffix_for_tool(TOOL_PIPELOCK_BLOCK))

    def test_unknown_tool_returns_txt(self):
        self.assertEqual(".txt", _suffix_for_tool("whatever"))


if __name__ == "__main__":
    unittest.main()
