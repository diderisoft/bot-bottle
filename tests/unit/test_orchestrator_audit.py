"""Unit: the orchestrator's audit events and the sender that delivers them."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from bot_bottle.contract.audit import AuditEvent
from bot_bottle.contract.auth import ROLE_CLI, ROLE_GATEWAY, mint
from bot_bottle.contract.http import ORCHESTRATOR_AUTH_HEADER
from bot_bottle.contract.supervise import TOOL_EGRESS_ALLOW
from bot_bottle.orchestrator.audit_events import OrchestratorAudit
from bot_bottle.orchestrator.audit_sender import AuditSender
from bot_bottle.orchestrator.hostctl_relay import HostControlUnreachable
from bot_bottle.orchestrator.server import create_app
from bot_bottle.orchestrator.service import OrchestratorCore
from bot_bottle.orchestrator.store.registry_store import RegistryStore
from bot_bottle.orchestrator.store.store_manager import StoreManager

_KEY = "audit-test-key"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.producers: list[str] = []

    def submit(self, event: AuditEvent, producer: str = "orchestrator") -> None:
        self.events.append(event)
        self.producers.append(producer)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


class ApiEvents(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db = root / "db" / "bot-bottle.db"
        db.parent.mkdir(parents=True)
        env = patch.dict("os.environ", {"BOT_BOTTLE_ROOT": str(root)})
        env.start()
        self.addCleanup(env.stop)
        self.store = RegistryStore(db)
        self.store.migrate()
        StoreManager(db).migrate()
        self.orch = OrchestratorCore(self.store)
        self.sink = _Sink()

    def call(self, method: str, path: str, body: object = None,
             role: str | None = ROLE_CLI) -> tuple[int, Any]:
        headers = {"content-type": "application/json"}
        if role is not None:
            headers[ORCHESTRATOR_AUTH_HEADER] = mint(role, _KEY)
        app = create_app(self.orch, signing_key=_KEY,
                         audit=OrchestratorAudit(self.sink, slug_for=self.orch.session_for))

        async def request() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://orchestrator") as client:
                return await client.request(method, path, headers=headers,
                                            content=b"" if body is None else json.dumps(body))
        response = asyncio.run(request())
        return response.status_code, response.json()

    def test_a_missing_token_is_a_rejected_token(self) -> None:
        self.assertEqual(401, self.call("GET", "/bottles", role=None)[0])
        [event] = self.sink.events  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual(("auth.token_rejected", "blocked", "GET /bottles"),
                         (event.type, event.outcome, event.action))

    def test_the_wrong_role_is_an_authz_denial(self) -> None:
        self.assertEqual(403, self.call("GET", "/bottles", role=ROLE_GATEWAY)[0])
        [event] = self.sink.events  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual(("auth.authz_denied", "role:gateway"), (event.type, event.actor))

    def test_registering_mints_a_token_recorded_by_fingerprint_only(self) -> None:
        status, body = self.call("POST", "/bottles", {
            "source_ip": "10.243.0.9", "metadata": json.dumps({"slug": "amber-fox"})})
        self.assertEqual(201, status)
        [event] = self.sink.events  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual("auth.token_minted", event.type)
        # Named by slug, like the host controller's events; the registry id is the run.
        self.assertEqual(("amber-fox", "amber-fox", body["bottle_id"]),
                         (event.bottle, event.resource, event.activation))
        assert event.payload is not None
        self.assertTrue(event.payload["token_id"].startswith("sha256:"))
        self.assertNotIn(body["identity_token"], json.dumps(event.to_json()))

    def test_an_unattributed_bottle_is_a_rejected_token(self) -> None:
        status, _ = self.call("POST", "/resolve",
                              {"source_ip": "10.243.0.9", "identity_token": "nope"},
                              role=ROLE_GATEWAY)
        self.assertEqual(403, status)
        [event] = self.sink.events  # pylint: disable=unbalanced-tuple-unpacking
        assert event.payload is not None
        self.assertEqual(("auth.token_rejected", "unattributed", "10.243.0.9"),
                         (event.type, event.payload["reason_code"], event.payload["src_ip"]))

    def test_the_gateway_relays_its_events_under_its_component(self) -> None:
        event = {"type": "egress.decision", "actor": "bottled-agent:a", "action": "egress.get",
                 "outcome": "allowed", "sensitivity": "security", "ts_event": "t"}
        status, body = self.call("POST", "/audit/events",
                                 {"producer": "egress", "event": event}, role=ROLE_GATEWAY)
        self.assertEqual((200, {"queued": True}), (status, body))
        self.assertEqual((["egress.decision"], ["egress"]),
                         (self.sink.types(), self.sink.producers))

    def test_a_relayed_event_must_name_a_gateway_component(self) -> None:
        event = {"type": "egress.decision", "actor": "a", "action": "x", "outcome": "allowed",
                 "sensitivity": "security", "ts_event": "t"}
        for producer, bad in (("hostctl", event), ("egress", {**event, "host": "spoofed"})):
            status, _ = self.call("POST", "/audit/events", {"producer": producer, "event": bad},
                                  role=ROLE_GATEWAY)
            self.assertEqual(400, status)
        self.assertEqual([], self.sink.events)

    def test_a_proposal_and_its_decision(self) -> None:
        rec = self.store.register("10.243.0.9", metadata=json.dumps({"slug": "demo"}),
                                  policy="routes: []\n")
        status, body = self.call("POST", "/supervise/propose", {
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "tool": TOOL_EGRESS_ALLOW, "proposed_file": "routes:\n  - host: g.com\n",
            "justification": "need it"}, role=ROLE_GATEWAY)
        self.assertEqual(201, status)
        status, _ = self.call("POST", "/supervise/respond", {
            "proposal_id": body["proposal_id"], "bottle_slug": rec.bottle_id,
            "decision": "reject", "notes": "no"})
        self.assertEqual(200, status)
        proposed, resolved = self.sink.events  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual(("decision.proposed", "deferred"), (proposed.type, proposed.outcome))
        self.assertEqual(("demo", "bottled-agent:demo"), (proposed.bottle, proposed.actor))
        self.assertEqual("demo", resolved.bottle)
        # The agent's reason is its own claim.
        self.assertEqual({"agent_rationale": "need it"}, proposed.untrusted)
        assert proposed.payload is not None
        self.assertNotIn("justification", proposed.payload)
        self.assertEqual(("decision.resolved", "blocked", "role:cli"),
                         (resolved.type, resolved.outcome, resolved.actor))
        self.assertEqual(proposed.correlation_id, resolved.correlation_id)


def _event(ts: str = "2026-10-23T00:00:00.000Z") -> AuditEvent:
    return AuditEvent(type="auth.token_rejected", actor="unauthenticated", action="GET /x",
                      outcome="blocked", sensitivity="security", ts_event=ts)


class _Relay:
    """Answers POST /audit/append with whatever `status` says."""

    def __init__(self) -> None:
        self.status: int | None = 200  # None: unreachable
        self.sent: list[dict[str, Any]] = []
        self.delivered = threading.Event()

    def request(self, method: str, path: str,
                body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        del method, path
        if self.status is None:
            raise HostControlUnreachable("down")
        if self.status == 200:
            assert body is not None
            self.sent.append(body)
            self.delivered.set()
        return self.status, {"error": "nope"} if self.status != 200 else {"written": True}


class Sender(unittest.TestCase):
    def sender(self, relay: _Relay, limit: int = 1000) -> AuditSender:
        sender = AuditSender(relay, limit=limit, retry_seconds=0.01)  # type: ignore[arg-type]
        self.addCleanup(sender.close, 1.0)
        return sender

    def wait_for(self, relay: _Relay, count: int) -> None:
        for _ in range(500):
            if len(relay.sent) >= count:
                return
            relay.delivered.wait(0.01)
        self.fail(f"only {len(relay.sent)} of {count} delivered")

    def test_delivers_in_order_with_the_producer(self) -> None:
        relay = _Relay()
        sender = self.sender(relay).start()
        sender.submit(_event("1"))
        sender.submit(_event("2"), producer="egress")
        self.wait_for(relay, 2)
        self.assertEqual([("orchestrator", "1"), ("egress", "2")],
                         [(b["producer"], b["event"]["ts_event"]) for b in relay.sent])

    def test_retries_while_the_host_controller_is_down(self) -> None:
        relay = _Relay()
        relay.status = None
        sender = self.sender(relay).start()
        sender.submit(_event())
        relay.delivered.wait(0.1)
        self.assertEqual(1, sender.pending())
        relay.status = 200
        self.wait_for(relay, 1)

    def test_a_refused_event_is_dropped_not_retried(self) -> None:
        relay = _Relay()
        relay.status = 422
        sender = self.sender(relay)
        with patch("sys.stderr"):
            sender.start()
            sender.submit(_event())
            for _ in range(200):
                if not sender.pending():
                    break
                relay.delivered.wait(0.01)
        self.assertEqual(0, sender.pending())
        self.assertEqual([], relay.sent)

    def test_overflow_is_recorded_once_delivery_resumes(self) -> None:
        relay = _Relay()
        relay.status = None
        sender = self.sender(relay, limit=2)
        for ts in ("1", "2", "3", "4"):
            sender.submit(_event(ts))  # 1 and 2 are dropped
        relay.status = 200
        sender.start()
        self.wait_for(relay, 3)
        events = [b["event"] for b in relay.sent]
        self.assertEqual("3", events[0]["ts_event"])
        truncation = events[1]
        self.assertEqual("audit.truncation_detected", truncation["type"])
        self.assertEqual({"dropped": 2, "ts_first": "1", "ts_last": "2"},
                         {k: truncation["payload"][k] for k in ("dropped", "ts_first", "ts_last")})
        self.assertEqual("4", events[2]["ts_event"])


if __name__ == "__main__":
    unittest.main()
