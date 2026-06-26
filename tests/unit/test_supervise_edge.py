"""Unit: supervise queue/audit error + edge branches (coverage ratchet,
ADR 0004). Complements test_supervise.py with the malformed-input and
fallback paths."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle import supervise
from bot_bottle.supervise import (
    Proposal,
    TOOL_EGRESS_ALLOW,
    list_pending_proposals,
    read_audit_entries,
    read_proposal,
    read_response,
    wait_for_response,
)


def _proposal() -> Proposal:
    return Proposal.new(
        bottle_slug="slug",
        tool=TOOL_EGRESS_ALLOW,
        proposed_file="x",
        justification="j",
        current_file_hash="h",
    )


class TestPathHelpers(unittest.TestCase):
    def test_bot_bottle_root(self) -> None:
        self.assertTrue(str(supervise.bot_bottle_root()).endswith(".bot-bottle"))

    def test_queue_dir_for_slug(self) -> None:
        self.assertIn("slug", str(supervise.queue_dir_for_slug("slug")))

    def test_id_from_non_proposal_filename(self) -> None:
        self.assertIsNone(supervise._id_from_proposal_filename(Path("x.response.json")))


class TestReadMalformed(unittest.TestCase):
    def test_read_proposal_non_dict(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "p.proposal.json").write_text("[]")
            with self.assertRaises(ValueError):
                read_proposal(Path(d), "p")

    def test_read_response_non_dict(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "p.response.json").write_text("[]")
            with self.assertRaises(ValueError):
                read_response(Path(d), "p")

    def test_list_pending_skips_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            qd = Path(d)
            (qd / "bad.proposal.json").write_text("{ not json")
            (qd / "arr.proposal.json").write_text("[]")
            (qd / "incomplete.proposal.json").write_text("{}")  # from_dict raises
            supervise.write_proposal(qd, _proposal())  # one valid
            pending = list_pending_proposals(qd)
            self.assertEqual(1, len(pending))
            self.assertEqual("slug", pending[0].bottle_slug)

    def test_list_pending_skips_when_response_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            qd = Path(d)
            p = _proposal()
            supervise.write_proposal(qd, p)
            (qd / f"{p.id}.response.json").write_text("{}")  # response exists -> skipped
            self.assertEqual([], list_pending_proposals(qd))


class TestWaitForResponse(unittest.TestCase):
    def test_malformed_response_then_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "p.response.json").write_text("{ not json")
            with self.assertRaises(TimeoutError):
                wait_for_response(Path(d), "p", deadline=time.monotonic())

    def test_incomplete_response_then_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "p.response.json").write_text("{}")  # dict but from_dict raises
            with self.assertRaises(TimeoutError):
                wait_for_response(Path(d), "p", deadline=time.monotonic())


class TestReadAuditEntries(unittest.TestCase):
    def test_missing_log_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                patch.dict("os.environ", {"HOME": home}):
            self.assertEqual([], read_audit_entries("egress", "nope"))

    def test_skips_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                patch.dict("os.environ", {"HOME": home}):
            path = supervise.audit_log_path("egress", "slug")
            path.parent.mkdir(parents=True, exist_ok=True)
            valid = (
                '{"timestamp": "t", "bottle_slug": "slug", "component": "egress",'
                ' "operator_action": "approve", "operator_notes": "",'
                ' "justification": "", "diff": ""}'
            )
            path.write_text(
                "\n"               # blank line skipped
                "{ not json\n"     # JSONDecodeError skipped
                "[]\n"             # not a dict skipped
                "{}\n"             # missing fields -> ValueError skipped
                + valid + "\n"
            )
            entries = read_audit_entries("egress", "slug")
            self.assertEqual(1, len(entries))
            self.assertEqual("approve", entries[0].operator_action)


class TestFlockFallback(unittest.TestCase):
    def test_flock_on_closed_fd_is_swallowed(self) -> None:
        # flock on a closed fd raises OSError(EBADF), which the helpers swallow.
        fd = os.open(os.devnull, os.O_RDONLY)
        os.close(fd)
        supervise._try_flock(fd)
        supervise._try_funlock(fd)


if __name__ == "__main__":
    unittest.main()
