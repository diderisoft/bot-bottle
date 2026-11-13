"""Unit: what the host controller records, and that auditing never blocks it."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from bot_bottle.contract.audit import AuditEvent, canonical
from bot_bottle.control.hostctl.audit_events import (
    AuditUnavailable, HostAudit, digest_content, digest_manifest, digest_request,
    manifest_content,
)
from bot_bottle.control.hostctl.audit_journal import AuditJournal, AuditJournalBusy
from bot_bottle.manifest.agent import ManifestAgent
from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.index import Manifest


class HostAuditTest(unittest.TestCase):
    def test_a_journal_that_will_not_open_turns_recording_off(self) -> None:
        err = io.StringIO()
        with mock.patch("bot_bottle.control.hostctl.audit_events.AuditJournal",
                        side_effect=AuditJournalBusy("held")), redirect_stderr(err):
            audit = HostAudit.open()
            audit.rejected("GET /x", role=None, request_digest="d")  # no-op, no raise
        self.assertIn("not recording", err.getvalue())

    def test_a_failed_write_is_logged_not_raised(self) -> None:
        journal = mock.Mock(spec=AuditJournal)
        journal.append.side_effect = OSError("disk full")
        err = io.StringIO()
        with redirect_stderr(err):
            HostAudit(journal).rejected("GET /x", role="gateway", request_digest="d")
        self.assertIn("disk full", err.getvalue())

    def test_records_carry_the_hostctl_producer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = AuditJournal(Path(tmp), host="h", engine="e")
            try:
                HostAudit(journal).rejected("GET /x", role="gateway", request_digest="d")
                last = journal.path.read_text().splitlines()[-1]
            finally:
                journal.close()
        self.assertIn('"producer":"hostctl"', last)
        self.assertIn('"type":"hostctl.broker_reject"', last)

    def test_listeners_hear_each_relayed_event_once(self) -> None:
        heard: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            journal = AuditJournal(Path(tmp), host="h", engine="e")
            try:
                audit = HostAudit(journal)
                audit.listen(lambda event, producer: heard.append((event.type, producer)))
                event = _event()
                audit.append(event, producer="egress")
                audit.append(event, producer="egress")  # a retry
            finally:
                journal.close()
        self.assertEqual([("egress.decision", "egress")], heard)

    def test_listeners_hear_events_the_journal_cannot_write(self) -> None:
        heard: list[str] = []
        audit = HostAudit(None)
        audit.listen(lambda event, producer: heard.append(producer))
        with self.assertRaises(AuditUnavailable):
            audit.append(_event(), producer="egress")
        self.assertEqual(["egress"], heard)

    def test_a_failing_listener_does_not_break_the_append(self) -> None:
        journal = mock.Mock(spec=AuditJournal)
        journal.append.return_value = {}
        audit = HostAudit(journal)
        audit.listen(mock.Mock(side_effect=RuntimeError("boom")))
        with redirect_stderr(io.StringIO()) as err:
            self.assertTrue(audit.append(_event(), producer="egress"))
        self.assertIn("boom", err.getvalue())


def _event() -> AuditEvent:
    return AuditEvent(type="egress.decision", actor="bottled-agent:b", action="egress.get",
                      outcome="blocked", sensitivity="security", ts_event="2026-10-24T00:00:00Z",
                      bottle="b", payload={"host": "x.example", "reason_code": "allowlist"})


class Digests(unittest.TestCase):
    def test_a_request_digest_leaves_out_secret_values(self) -> None:
        a = digest_request({"agent": "demo", "secrets": {"TOKEN": "one"}})
        b = digest_request({"agent": "demo", "secrets": {"TOKEN": "two"}})
        c = digest_request({"agent": "demo", "secrets": {"OTHER": "one"}})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_an_unrenderable_manifest_digests_to_empty(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual("", digest_manifest(mock.Mock(spec=[])))
        self.assertIn("couldn't record the manifest", err.getvalue())


def _manifest(bottle: dict[str, object], agent: dict[str, object] | None = None) -> Manifest:
    return Manifest(agent=ManifestAgent.from_dict("demo", agent or {}),
                    bottle=ManifestBottle.from_dict("dev", bottle))


_ROUTE = {"host": "api.example", "auth": {"type": "bearer", "value": "secret:TOK"}}


class WhatRan(unittest.TestCase):
    """A launch keeps what it ran: the whole resolved bottle and agent."""

    def setUp(self) -> None:
        self.enterContext(mock.patch("bot_bottle.manifest.versions.secret_values",
                                     return_value={"TOK": "s3cr3t-value-1234"}))

    def test_the_content_is_the_whole_bottle_and_agent(self) -> None:
        content = manifest_content(_manifest(
            {"egress": {"routes": [{**_ROUTE, "methods": ["GET", "POST"]}]},
             "env": {"MODE": "fast"}},
            {"prompt": "Do the work."}))
        self.assertEqual("api.example", content["bottle"]["egress"]["routes"][0]["host"])
        self.assertEqual("secret:TOK", content["bottle"]["egress"]["routes"][0]["auth"]["value"])
        self.assertEqual("Do the work.", content["agent"]["prompt"])

    def test_the_digest_follows_any_change_to_the_policy(self) -> None:
        read = digest_manifest(_manifest({"egress": {"routes": [_ROUTE]}}))
        write = digest_manifest(_manifest({"egress": {"routes": [
            {**_ROUTE, "methods": ["GET", "HEAD", "POST"]}]}}))
        self.assertTrue(read.startswith("sha256:"))
        self.assertNotEqual(read, write)  # the old preview digest missed this
        self.assertEqual(read, digest_manifest(_manifest({"egress": {"routes": [_ROUTE]}})))
        self.assertEqual(read, digest_content(manifest_content(
            _manifest({"egress": {"routes": [_ROUTE]}}))))

    def test_a_pasted_secret_value_is_kept_out(self) -> None:
        content = manifest_content(_manifest({"env": {"KEY": "s3cr3t-value-1234"}}))
        self.assertEqual("[secret:TOK]", content["bottle"]["env"]["KEY"])
        self.assertNotIn("s3cr3t-value-1234", canonical(content))

    def test_the_journal_takes_it(self) -> None:
        # A credential-shaped key (the agent's auth_token, a secret's name)
        # is written <key>_ref, or the journal would refuse the whole start.
        content = manifest_content(_manifest(
            {"egress": {"routes": [_ROUTE]}},
            {"agent_provider": {"harness": "claude", "login": {"oauth_token": "secret:OAUTH"}}}))
        self.assertIn("auth_token_ref", content["agent"]["agent_provider"])
        with tempfile.TemporaryDirectory() as tmp:
            journal = AuditJournal(Path(tmp), host="h", engine="e")
            try:
                HostAudit(journal).bottled_agent_start(
                    bottle="b1", activation="a1", manifest_digest=digest_content(content),
                    backend="docker", agent="demo", manifest=content)
                record = json.loads(journal.path.read_text().splitlines()[-1])
            finally:
                journal.close()
        self.assertEqual(content, record["payload"]["manifest"])
        self.assertEqual(digest_content(record["payload"]["manifest"]), record["manifest_digest"])


if __name__ == "__main__":
    unittest.main()
