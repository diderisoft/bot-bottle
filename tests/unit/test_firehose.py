"""Unit: the request firehose, from gateway capture to the host controller's
fan-out and `audit tail` (PRD 0093)."""

from __future__ import annotations

import json
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from bot_bottle.cli.commands.audit import tail_line
from bot_bottle.contract.auth import ROLE_CLI, ROLE_GATEWAY
from bot_bottle.contract.firehose import FIREHOSE_PATH, FIREHOSE_STREAM_PATH, Capture
from bot_bottle.contract.hostctl import HOST_CONTROL, ROLE_ORCHESTRATOR, bearer
from bot_bottle.control.hostctl.firehose import FirehoseHub
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.gateway.egress.firehose import (
    EgressFirehose,
    FirehoseClient,
    body_capture,
    http_activity,
    snapshot,
)

_SCHEMA = json.loads((Path(__file__).parent / "data" / "ocsf_1_9_0_subset.json").read_text())
_SECRET = "s3cret-value-for-this-bottle"


class _Headers(dict[str, str]):
    pass


def _flow(method: str = "POST", host: str = "api.example.com", path: str = "/v1?x=1",
          headers: dict[str, str] | None = None, body: bytes = b"") -> Any:
    request = types.SimpleNamespace(
        method=method, scheme="https", pretty_host=host, port=443, path=path,
        headers=_Headers(headers or {}), get_content=lambda strict=False: body)
    return types.SimpleNamespace(request=request, response=None, metadata={},
                                 client_conn=types.SimpleNamespace(peername=("10.243.0.9", 1)))


class CaptureContract(unittest.TestCase):
    def test_a_malformed_reply_means_nothing_is_wanted(self) -> None:
        self.assertEqual(Capture(wanted=False), Capture.from_json({"wanted": "yes"}))
        self.assertEqual(Capture(wanted=True, max_body_bytes=10),
                         Capture.from_json({"wanted": True, "max_body_bytes": 10}))


class Snapshot(unittest.TestCase):
    def test_credentials_are_dropped_and_secrets_scrubbed(self) -> None:
        flow = _flow(headers={"Authorization": "Bearer x", "Cookie": "c=1",
                              "X-Note": f"key {_SECRET}"}, body=f"data={_SECRET}".encode())
        snap = snapshot(flow, {"EGRESS_TOKEN_API": _SECRET}, Capture(wanted=True))
        names = [h["name"] for h in cast(list[dict[str, str]], snap["headers"])]
        self.assertEqual(["X-Note"], names)
        self.assertNotIn(_SECRET, json.dumps(snap))

    def test_a_long_body_keeps_its_head_and_the_hash_of_the_whole(self) -> None:
        captured = body_capture(b"x" * 100, 10, {})
        self.assertEqual(("x" * 10, 100, True), (captured["text"], captured["bytes"],
                                                 captured["truncated"]))
        self.assertEqual(64, len(str(captured["sha256"])))
        self.assertNotIn("truncated", body_capture(b"short", 10, {}))

    def test_a_metadata_only_bottle_keeps_just_where_it_went(self) -> None:
        flow = _flow(headers={"X-Note": "hi"}, path="/private/report?id=7", body=b"data")
        snap = snapshot(flow, {}, Capture(wanted=True, response_bodies=True),
                        metadata_only=True)
        self.assertEqual({"method", "scheme", "host", "port", "started", "metadata_only"},
                         set(snap))
        record = http_activity(snap, slug="amber-fox", src_ip="", decision="allowed",
                               status=200)
        extra = cast(dict[str, dict[str, object]], record["unmapped"])["bot_bottle"]
        self.assertEqual("metadata", extra["firehose"])
        self.assertNotIn("private", json.dumps(record))

    def test_bodies_can_be_left_out(self) -> None:
        snap = snapshot(_flow(body=b"x"), {}, Capture(wanted=True, request_bodies=False))
        self.assertNotIn("body", snap)


class Records(unittest.TestCase):
    def test_a_record_is_a_valid_http_activity_event(self) -> None:
        snap = snapshot(_flow(), {}, Capture(wanted=True))
        record = http_activity(snap, slug="amber-fox", src_ip="10.243.0.9",
                               decision="blocked", reason="not in allowlist", status=403)
        spec = _SCHEMA["classes"]["http_activity"]
        for field in spec["required"]:
            self.assertIn(field, record)
        self.assertEqual(set(), set(record) - set(spec["attributes"]))
        for enum, allowed in spec["enums"].items():
            if enum in record:
                self.assertIn(record[enum], allowed, enum)
        self.assertEqual(6, record["activity_id"])  # POST
        self.assertEqual("1.9.0", cast(dict[str, object], record["metadata"])["version"])

    def test_the_tail_line(self) -> None:
        record = http_activity(snapshot(_flow(), {}, Capture(wanted=True)), slug="amber-fox",
                               src_ip="", decision="allowed", status=200)
        self.assertEqual("amber-fox  POST https://api.example.com:443/v1  200  allowed",
                         tail_line(record))
        self.assertEqual("... 3 record(s) dropped", tail_line({"firehose_dropped": 3}))


class _Client:
    """Stands in for FirehoseClient."""

    def __init__(self, capture: Capture) -> None:
        self.capture = capture
        self.records: list[dict[str, Any]] = []

    def submit(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class Hooks(unittest.TestCase):
    def test_nothing_is_captured_unless_wanted(self) -> None:
        client = _Client(Capture(wanted=False))
        firehose = EgressFirehose(client)  # type: ignore[arg-type]
        flow = _flow()
        firehose.begin(flow, {})
        firehose.end(flow, "amber-fox")
        self.assertEqual([], client.records)

    def test_each_request_is_recorded_once(self) -> None:
        client = _Client(Capture(wanted=True))
        firehose = EgressFirehose(client)  # type: ignore[arg-type]
        flow = _flow()
        firehose.begin(flow, {})
        firehose.mark(flow, "redacted")
        firehose.end(flow, "amber-fox")
        firehose.end(flow, "amber-fox", "blocked")  # e.g. the response hook after a block
        [record] = client.records  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual("redacted", record["unmapped"]["bot_bottle"]["decision"])

    def test_a_metadata_only_bottle_never_ships_a_response_body(self) -> None:
        client = _Client(Capture(wanted=True, response_bodies=True))
        firehose = EgressFirehose(client)  # type: ignore[arg-type]
        flow = _flow()
        firehose.begin(flow, {}, metadata_only=True)
        flow.response = types.SimpleNamespace(
            status_code=200, get_content=lambda strict=False: b"secret report")
        firehose.end(flow, "amber-fox")
        self.assertNotIn("response_body", client.records[0]["unmapped"]["bot_bottle"])
        self.assertEqual(13, client.records[0]["http_response"]["length"])

    def test_response_bodies_only_when_asked(self) -> None:
        for wanted in (False, True):
            client = _Client(Capture(wanted=True, response_bodies=wanted))
            firehose = EgressFirehose(client)  # type: ignore[arg-type]
            flow = _flow()
            firehose.begin(flow, {})
            flow.response = types.SimpleNamespace(
                status_code=200, get_content=lambda strict=False: b"hello")
            firehose.end(flow, "amber-fox")
            extra = client.records[0]["unmapped"]["bot_bottle"]
            self.assertEqual(wanted, "response_body" in extra)
            self.assertEqual(5, client.records[0]["http_response"]["length"])


class Hub(unittest.TestCase):
    def test_wanted_only_while_someone_listens(self) -> None:
        hub = FirehoseHub()
        self.assertEqual(Capture(wanted=False), hub.publish([{"x": 1}]))
        records = hub.subscribe(poll_seconds=0.01)
        next(records)  # subscribed; nothing yet
        self.assertTrue(hub.publish([{"x": 1}]).wanted)
        self.assertEqual({"x": 1}, next(r for r in records if r is not None))
        records.close()
        self.assertFalse(hub.wanted())

    def test_filters_and_gaps(self) -> None:
        hub = FirehoseHub()
        records = hub.subscribe(bottle="amber-fox", poll_seconds=0.01)
        next(records)
        hub.publish([{"actor": {"app_name": "other"}},
                     {"actor": {"app_name": "amber-fox"}}], dropped=2)
        got = [next(r for r in records if r is not None) for _ in range(2)]
        self.assertEqual([{"firehose_dropped": 2}, {"actor": {"app_name": "amber-fox"}}], got)
        records.close()


class Routes(unittest.TestCase):
    def setUp(self) -> None:
        self.key = "k" * 32
        self.hub = FirehoseHub()
        self.server = HostControlServer(("127.0.0.1", 0), "b", self.key, firehose=self.hub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path: str, role: str, body: object = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        return urllib.request.Request(self.url + path, data=data,
                                      headers=bearer(HOST_CONTROL.mint(role, self.key)))

    def test_publish_is_for_the_orchestrator_and_replies_with_the_capture(self) -> None:
        req = self.request(FIREHOSE_PATH, ROLE_ORCHESTRATOR, {"records": [], "dropped": 0})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual({"wanted": False}, {"wanted": json.loads(resp.read())["wanted"]})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(  # pylint: disable=consider-using-with
                self.request(FIREHOSE_PATH, ROLE_GATEWAY, {"records": []}), timeout=5)
        self.assertEqual(403, cm.exception.code)
        cm.exception.close()

    def test_the_stream_carries_published_records(self) -> None:
        resp = urllib.request.urlopen(  # pylint: disable=consider-using-with
            self.request(FIREHOSE_STREAM_PATH + "?host=api.example.com", ROLE_CLI), timeout=5)
        self.addCleanup(resp.close)
        for _ in range(100):
            if self.hub.wanted():
                break
            threading.Event().wait(0.02)
        self.hub.publish([{"dst_endpoint": {"hostname": "elsewhere"}},
                          {"dst_endpoint": {"hostname": "api.example.com"}}])
        line = b""
        while not line.strip():
            line = resp.readline()
        self.assertEqual({"dst_endpoint": {"hostname": "api.example.com"}}, json.loads(line))


class _Orchestrator(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.reply: dict[str, Any] = {"wanted": True}
        self.batches: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # pylint: disable=invalid-name
        server: Any = self.server
        server.batches.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        data = json.dumps(server.reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        del format, args


class Client(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Orchestrator()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = FirehoseClient(f"http://127.0.0.1:{self.server.server_address[1]}")

    def test_the_first_flush_asks_and_learns_what_to_capture(self) -> None:
        self.assertFalse(self.client.capture.wanted)
        self.client.flush()
        self.assertTrue(self.client.capture.wanted)
        self.assertEqual([{"records": [], "dropped": 0}], self.server.batches)

    def test_records_go_in_batches_and_an_idle_wanted_stream_stays_quiet(self) -> None:
        self.client.flush()
        self.client.submit({"n": 1})
        self.client.flush()
        self.client.flush()  # nothing queued, already wanted: no request
        self.assertEqual([{"n": 1}], self.server.batches[-1]["records"])
        self.assertEqual(2, len(self.server.batches))

    def test_a_failed_send_pauses_capture_and_counts_the_loss(self) -> None:
        self.client.flush()
        self.client.submit({"n": 1})
        self.server.shutdown()
        self.server.server_close()
        self.client.flush()
        self.assertFalse(self.client.capture.wanted)


if __name__ == "__main__":
    unittest.main()
