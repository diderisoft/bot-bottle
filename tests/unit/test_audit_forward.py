"""Unit: forwarding the audit journal to an HTTPS endpoint, at least once."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bot_bottle.contract.audit import AuditEvent
from bot_bottle.control.hostctl.audit_forward import (
    AuditForwarder,
    Cursor,
    ForwardCursors,
    read_batch,
)
from bot_bottle.control.hostctl.audit_journal import AuditJournal
from bot_bottle.lib.config_store import AuditForward
from bot_bottle.secret_source import MappingSecrets


class _Endpoint(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.status = 200
        self.batches: list[tuple[str, list[dict[str, Any]]]] = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # pylint: disable=invalid-name
        server: Any = self.server
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if server.status == 200:
            server.batches.append((self.headers.get("Authorization", ""),
                                   [json.loads(line) for line in body.splitlines()]))
        self.send_response(server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        del format, args


def _event() -> AuditEvent:
    return AuditEvent(type="egress.decision", actor="a", action="egress.get",
                      outcome="allowed", sensitivity="security",
                      ts_event="2026-10-23T00:00:00.000Z")


class Forwarding(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.dir = root / "audit"
        self.journal = AuditJournal(self.dir, host="h", engine="e")
        self.addCleanup(self.journal.close)
        self.endpoint = _Endpoint()
        threading.Thread(target=self.endpoint.serve_forever, daemon=True).start()
        self.addCleanup(self.endpoint.server_close)
        self.addCleanup(self.endpoint.shutdown)
        self.config: AuditForward | None = AuditForward(
            url=f"http://127.0.0.1:{self.endpoint.server_address[1]}/in",
            secret="SIEM_TOKEN", format="native")
        self.failures: list[AuditEvent] = []
        self.forwarder = AuditForwarder(
            self.dir, cursors=ForwardCursors(root / "db" / "bot-bottle.db"),
            config=lambda: self.config, secrets=MappingSecrets({"SIEM_TOKEN": "tok-123"}),
            record_failure=self.failures.append, now=lambda: "t")

    def sent(self) -> list[dict[str, Any]]:
        return [r for _, batch in self.endpoint.batches for r in batch]

    def test_sends_each_record_once_as_ndjson_with_the_bearer_token(self) -> None:
        self.journal.append(_event(), producer="egress")
        self.assertEqual(2, self.forwarder.forward_once())  # segment_open + the event
        self.assertEqual(0, self.forwarder.forward_once())
        self.journal.append(_event(), producer="egress")
        self.assertEqual(1, self.forwarder.forward_once())
        lines = self.journal.path.read_text().splitlines()
        self.assertEqual([json.loads(line) for line in lines], self.sent())
        self.assertEqual("Bearer tok-123", self.endpoint.batches[0][0])

    def test_ocsf_format_projects_each_record(self) -> None:
        assert self.config is not None
        self.config = AuditForward(self.config.url, "", "ocsf")
        self.journal.append(_event(), producer="egress")
        self.forwarder.forward_once()
        self.assertEqual("1.9.0", self.sent()[-1]["metadata"]["version"])
        self.assertEqual("", self.endpoint.batches[0][0])  # no secret, no token

    def test_a_failure_keeps_the_cursor_and_records_the_streak_once(self) -> None:
        self.endpoint.status = 500
        self.assertIsNone(self.forwarder.forward_once())
        self.assertIsNone(self.forwarder.forward_once())
        self.assertEqual(["audit.export_failed"], [e.type for e in self.failures])
        self.endpoint.status = 200
        self.assertEqual(1, self.forwarder.forward_once())  # resent from the cursor
        self.endpoint.status = 500
        self.forwarder.forward_once()
        self.journal.append(_event(), producer="egress")
        self.forwarder.forward_once()
        self.assertEqual(2, len(self.failures))  # a new streak after a success

    def test_a_new_endpoint_gets_the_journal_from_the_start(self) -> None:
        self.forwarder.forward_once()
        assert self.config is not None
        self.config = AuditForward(self.config.url + "/other", "", "native")
        self.assertEqual(1, self.forwarder.forward_once())

    def test_nothing_is_sent_when_forwarding_is_off(self) -> None:
        self.config = None
        self.assertEqual(0, self.forwarder.forward_once())
        self.assertEqual([], self.endpoint.batches)


class ReadBatch(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        journal = AuditJournal(self.dir, host="h", engine="e")
        for _ in range(4):
            journal.append(_event(), producer="egress")
        journal.close()
        self.path = journal.path

    def test_a_torn_tail_waits_for_the_next_read(self) -> None:
        with self.path.open("a") as f:
            f.write('{"id":"half')
        lines, after = read_batch(self.dir, Cursor())
        self.assertEqual(5, len(lines))
        self.assertEqual(([], after), read_batch(self.dir, after))

    def test_a_limit_resumes_where_it_stopped(self) -> None:
        first, after = read_batch(self.dir, Cursor(), limit=2)
        rest, _ = read_batch(self.dir, after)
        self.assertEqual(self.path.read_text().splitlines(), first + rest)

    def test_continues_into_later_segments(self) -> None:
        lines = self.path.read_text().splitlines()
        self.path.write_text("\n".join(lines[:3]) + "\n")
        (self.dir / "29990101T000000Z.jsonl").write_text("\n".join(lines[3:]) + "\n")
        got, _ = read_batch(self.dir, Cursor())
        self.assertEqual(lines, got)


if __name__ == "__main__":
    unittest.main()
