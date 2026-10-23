"""Unit: the request firehose's configured sinks (PRD 0093)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import firehose_sinks
from bot_bottle.control.hostctl.firehose import FirehoseHub
from bot_bottle.control.hostctl.firehose_sinks import ExecSink, HttpsSink, SinkSet
from bot_bottle.lib.config_store import FirehoseSettings, FirehoseSink


def _wait(check: Any, seconds: float = 10.0) -> bool:
    idle = threading.Event()
    for _ in range(int(seconds / 0.02)):
        if check():
            return True
        idle.wait(0.02)
    return False


class _Endpoint(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.status = 200
        self.lines: list[dict[str, Any]] = []
        self.auth: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # pylint: disable=invalid-name
        server: Any = self.server
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if server.status == 200:
            server.auth.append(self.headers.get("Authorization", ""))
            server.lines.extend(json.loads(line) for line in body.splitlines())
        self.send_response(server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        del format, args


class Exec(unittest.TestCase):
    def test_records_reach_the_commands_stdin_as_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.jsonl"
            script = ("import sys\n"
                      f"with open({str(out)!r}, 'a') as f:\n"
                      "    for line in sys.stdin:\n"
                      "        f.write(line); f.flush()\n")
            sink = ExecSink(FirehoseSink("exec", command=(sys.executable, "-c", script))).start()
            self.addCleanup(sink.close)
            sink.offer({"n": 1})
            sink.offer({"n": 2})
            self.assertTrue(_wait(lambda: out.exists() and len(out.read_text().splitlines()) == 2))
            self.assertEqual([{"n": 1}, {"n": 2}],
                             [json.loads(line) for line in out.read_text().splitlines()])

    def test_a_command_that_wont_start_doesnt_crash_the_sink(self) -> None:
        with mock.patch.object(firehose_sinks, "warn"):
            sink = ExecSink(FirehoseSink("exec", command=("/no/such/command",))).start()
            sink.offer({"n": 1})
            sink.close()


class Https(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = _Endpoint()
        threading.Thread(target=self.endpoint.serve_forever, daemon=True).start()
        self.addCleanup(self.endpoint.server_close)
        self.addCleanup(self.endpoint.shutdown)
        self.url = f"http://127.0.0.1:{self.endpoint.server_address[1]}/in"

    def test_batches_go_out_as_ndjson_with_the_bearer_token(self) -> None:
        sink = HttpsSink(FirehoseSink("https", url=self.url, secret="S"), lambda: "tok").start()
        self.addCleanup(sink.close)
        sink.offer({"n": 1})
        self.assertTrue(_wait(lambda: self.endpoint.lines == [{"n": 1}]))
        self.assertEqual("Bearer tok", self.endpoint.auth[0])

    def test_a_failed_batch_is_dropped_and_reported_in_the_stream(self) -> None:
        self.endpoint.status = 500
        sink = HttpsSink(FirehoseSink("https", url=self.url), lambda: "").start()
        self.addCleanup(sink.close)
        sink.offer({"n": 1})
        _wait(lambda: False, 1.5)
        self.endpoint.status = 200
        sink.offer({"n": 2})
        self.assertTrue(_wait(lambda: {"n": 2} in self.endpoint.lines))
        self.assertEqual([{"firehose_dropped": 1}, {"n": 2}], self.endpoint.lines)


class Set(unittest.TestCase):
    def test_follows_the_configuration(self) -> None:
        sinks = SinkSet(lambda name: None)
        self.addCleanup(sinks.close)
        a = FirehoseSink("https", url="http://127.0.0.1:9/a")
        b = FirehoseSink("https", url="http://127.0.0.1:9/b")
        self.assertFalse(sinks.any())
        sinks.reconcile((a, b))
        first = dict(sinks._sinks)  # pylint: disable=protected-access
        sinks.reconcile((b,))
        self.assertEqual([b], list(sinks._sinks))  # pylint: disable=protected-access
        # Kept running, not restarted.
        self.assertIs(first[b], sinks._sinks[b])  # pylint: disable=protected-access


class HubWithSinks(unittest.TestCase):
    def test_a_configured_sink_counts_as_listening(self) -> None:
        settings = FirehoseSettings(sinks=(FirehoseSink("https", url="http://127.0.0.1:9/x"),),
                                    response_bodies=True, max_body_bytes=10)
        sinks = SinkSet(lambda name: None)
        hub = FirehoseHub(settings=lambda: settings, sinks=sinks)
        self.addCleanup(hub.close)
        reply = hub.reply()
        self.assertEqual((True, True, 10),
                         (reply.wanted, reply.response_bodies, reply.max_body_bytes))

    def test_nothing_configured_nothing_wanted(self) -> None:
        hub = FirehoseHub(settings=FirehoseSettings, sinks=SinkSet(lambda name: None))
        self.assertFalse(hub.reply().wanted)


if __name__ == "__main__":
    unittest.main()
