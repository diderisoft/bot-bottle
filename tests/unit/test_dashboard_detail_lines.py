"""Unit: dashboard's detail-view line builder.

_detail_lines returns (text, attr) tuples. Most are plain; for
pipelock-block proposals it appends a "→ would allow host: <host>"
line tagged with the green attr so the operator sees at a glance
which hostname will land in pipelock's allowlist on approval."""

import unittest

from claude_bottle import supervise
from claude_bottle.cli import dashboard
from claude_bottle.supervise import (
    Proposal,
    TOOL_CAPABILITY_BLOCK,
    TOOL_CRED_PROXY_BLOCK,
    TOOL_PIPELOCK_BLOCK,
    sha256_hex,
)


def _qp(tool: str, payload: str) -> dashboard.QueuedProposal:
    from datetime import datetime, timezone
    from pathlib import Path
    p = Proposal.new(
        bottle_slug="dev",
        tool=tool,
        proposed_file=payload,
        justification="needs",
        current_file_hash=sha256_hex(payload),
        now=datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc),
    )
    return dashboard.QueuedProposal(proposal=p, queue_dir=Path("/tmp/q"))


class TestPipelockHostHighlight(unittest.TestCase):
    GREEN = 0xDEADBEEF  # arbitrary sentinel; _detail_lines passes through

    def test_appends_green_host_line_for_pipelock_block(self):
        lines = dashboard._detail_lines(
            _qp(TOOL_PIPELOCK_BLOCK, "https://api.github.com/repos/foo/bar"),
            green_attr=self.GREEN,
        )
        host_lines = [
            (text, attr) for text, attr in lines
            if text.startswith("→ would allow host:")
        ]
        self.assertEqual(1, len(host_lines))
        text, attr = host_lines[0]
        self.assertEqual("→ would allow host: api.github.com", text)
        self.assertEqual(self.GREEN, attr)

    def test_no_host_line_for_cred_proxy_block(self):
        lines = dashboard._detail_lines(
            _qp(TOOL_CRED_PROXY_BLOCK, '{"routes": []}'),
            green_attr=self.GREEN,
        )
        self.assertFalse(any("would allow host" in t for t, _ in lines))

    def test_no_host_line_for_capability_block(self):
        lines = dashboard._detail_lines(
            _qp(TOOL_CAPABILITY_BLOCK, "FROM python:3.13\n"),
            green_attr=self.GREEN,
        )
        self.assertFalse(any("would allow host" in t for t, _ in lines))

    def test_skips_host_line_when_url_unparseable(self):
        # Shouldn't happen in production — supervise_server validates
        # the URL before queuing — but if a malformed payload ever
        # reaches the dashboard, don't add a misleading host line.
        lines = dashboard._detail_lines(
            _qp(TOOL_PIPELOCK_BLOCK, "garbage-not-a-url"),
            green_attr=self.GREEN,
        )
        self.assertFalse(any("would allow host" in t for t, _ in lines))

    def test_no_green_attr_passed_still_renders_host(self):
        # Even without color support (green_attr=0), the host line
        # is still present — it just won't be coloured.
        lines = dashboard._detail_lines(
            _qp(TOOL_PIPELOCK_BLOCK, "https://api.github.com/x"),
            green_attr=0,
        )
        host_lines = [t for t, _ in lines if t.startswith("→ would allow host:")]
        self.assertEqual(["→ would allow host: api.github.com"], host_lines)


class TestFailedUrlHost(unittest.TestCase):
    def test_extracts_hostname(self):
        self.assertEqual(
            "api.github.com",
            dashboard._failed_url_host("https://api.github.com/repos/foo"),
        )

    def test_returns_empty_for_unparseable(self):
        self.assertEqual("", dashboard._failed_url_host("not a url"))

    def test_returns_empty_for_url_without_host(self):
        self.assertEqual("", dashboard._failed_url_host("https:///nohost"))


if __name__ == "__main__":
    unittest.main()
