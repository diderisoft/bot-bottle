"""Unit: `bot-bottle audit`."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands.audit import cmd_audit
from bot_bottle.contract.audit import AuditEvent, canonical
from bot_bottle.contract.hostctl import HOST_CONFIG_PATH, HOST_CONTROL, ROLE_ORCHESTRATOR
from bot_bottle.control.hostctl import runner
from bot_bottle.control.hostctl.audit_journal import AuditJournal
from bot_bottle.control.hostctl.client import HostControlClient, HostControlRefused
from bot_bottle.lib.db_store import DbStore
from bot_bottle.paths import host_audit_dir, host_db_path
from bot_bottle.trust_domain import signing_key

from . import serve_host_controller, use_bottle_root


class AuditCommand(unittest.TestCase):
    """Over a real host controller's routes: the index and settings are in the
    host DB, which only the host controller opens."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(tmp.name)))
        self.dir = host_audit_dir()
        serve_host_controller(self)
        # Every host DB connection, by the thread that opened it.
        self.opened_by: list[threading.Thread] = []
        connect = DbStore._connect  # pylint: disable=protected-access

        def recording(store: DbStore) -> sqlite3.Connection:
            self.opened_by.append(threading.current_thread())
            return connect(store)
        self.enterContext(mock.patch.object(DbStore, "_connect", recording))

    def run_cmd(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = cmd_audit(list(args))
        return rc, out.getvalue()

    def test_the_cli_never_opens_the_host_db_itself(self) -> None:
        self.journal()
        for args in (("delivery", "required"), ("query",), ("rebuild",), ("firehose",)):
            self.assertEqual(0, self.run_cmd(*args)[0], args)
        self.assertTrue(self.opened_by)
        self.assertNotIn(threading.main_thread(), self.opened_by)
        self.assertTrue(host_db_path().is_file())

    def test_a_bad_setting_is_refused_by_the_host_controller(self) -> None:
        client = HostControlClient(runner.ensure_running(), runner.cli_token())
        bodies: list[dict[str, object]] = [
            {"transcript_retention_days": 0}, {"audit_delivery": "sometimes"},
            {"no_such_setting": 1}]
        errors: list[str] = []
        for body in bodies:
            with self.assertRaises(HostControlRefused) as cm:
                client.post(HOST_CONFIG_PATH, body)
            self.assertEqual(400, cm.exception.code, body)
            errors.append(cm.exception.error)
        self.assertIn("at least 1", errors[0])
        self.assertIn("unknown host setting", errors[-1])

    def test_another_role_cant_reach_host_state(self) -> None:
        client = HostControlClient(runner.ensure_running(),
                                   HOST_CONTROL.mint(ROLE_ORCHESTRATOR, signing_key(HOST_CONTROL)))
        with self.assertRaises(HostControlRefused) as cm:
            client.get(HOST_CONFIG_PATH)
        self.assertEqual(403, cm.exception.code)

    def journal(self) -> AuditJournal:
        j = AuditJournal(self.dir, host="h", engine="e")
        j.append(AuditEvent(type="egress.decision", actor="a", action="egress.connect",
                            outcome="blocked", sensitivity="security", ts_event="t"),
                 producer="egress")
        return j

    def test_delivery_shows_and_sets_the_host_default(self) -> None:
        self.assertEqual((0, "best_effort\n"), self.run_cmd("delivery"))
        self.assertEqual((0, "required\n"), self.run_cmd("delivery", "required"))
        self.assertEqual((0, "required\n"), self.run_cmd("delivery"))

    def test_retention_keeps_every_segment_until_set(self) -> None:
        self.assertEqual((0, "keeping every segment\n"), self.run_cmd("retention"))
        self.assertEqual((0, "keeping up to 90 days\n"), self.run_cmd("retention", "--days", "90"))
        self.assertEqual((0, "keeping up to 90 days and 4 GiB\n"),
                         self.run_cmd("retention", "--max-gb", "4"))
        self.assertEqual((0, "keeping up to 4 GiB\n"), self.run_cmd("retention", "--days", "0"))
        self.assertEqual(2, self.run_cmd("retention", "--days", "-1")[0])

    def test_forward_shows_sets_and_turns_off(self) -> None:
        self.assertEqual((0, "not forwarding\n"), self.run_cmd("forward"))
        rc, out = self.run_cmd("forward", "--url", "https://siem.example/in",
                               "--secret", "SIEM_TOKEN", "--format", "ocsf")
        self.assertEqual(0, rc)
        self.assertIn("forwarding ocsf to https://siem.example/in", out)
        self.assertIn("secret SIEM_TOKEN", out)
        self.assertEqual((0, "not forwarding\n"), self.run_cmd("forward", "--off"))

    def test_firehose_shows_and_edits_sinks(self) -> None:
        rc, out = self.run_cmd("firehose")
        self.assertEqual(0, rc)
        self.assertIn("no sinks", out)
        rc, out = self.run_cmd("firehose", "--exec", "vector --config /etc/v.toml",
                               "--https", "https://siem.example/in", "--secret", "SIEM",
                               "--response-bodies", "on")
        self.assertIn("1. exec: vector --config /etc/v.toml", out)
        self.assertIn("2. https: https://siem.example/in (bearer token from secret SIEM)", out)
        self.assertIn("response bodies on", out)
        rc, out = self.run_cmd("firehose", "--remove", "1")
        self.assertNotIn("exec", out)
        self.assertEqual(1, self.run_cmd("firehose", "--remove", "5")[0])

    def test_no_journal_is_not_an_error(self) -> None:
        for action in ("verify", "query", "rebuild"):
            rc, out = self.run_cmd(action)
            self.assertEqual(rc, 0)
            self.assertIn("no audit journal", out)

    def test_verify_exits_non_zero_on_a_break(self) -> None:
        j = self.journal()
        self.assertEqual(self.run_cmd("verify")[0], 0)
        j.path.write_text(j.path.read_text().replace("blocked", "allowed"))
        rc, out = self.run_cmd("verify")
        self.assertEqual(rc, 1)
        self.assertIn("edited", out)

    def test_import_then_verify_covers_the_imported_chain(self) -> None:
        other = AuditJournal(self.dir.parent / "other", host="there", engine="e")
        self.journal()
        rc, out = self.run_cmd("import", str(other.path))
        self.assertEqual(rc, 0, out)
        self.assertIn("new chain", out)
        rc, out = self.run_cmd("verify")
        self.assertEqual(rc, 0)
        self.assertIn("imported there/", out)

    def test_query_json_prints_journal_lines_verbatim(self) -> None:
        j = self.journal()
        rc, out = self.run_cmd("query", "--type", "egress.*", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(out, j.path.read_text().splitlines()[-1] + "\n")
        self.assertEqual(out.strip(), canonical(json.loads(out)))

    def test_export_writes_native_or_ocsf_jsonl(self) -> None:
        j = self.journal()
        rc, out = self.run_cmd("export", "--type", "egress.*")
        self.assertEqual((0, j.path.read_text().splitlines()[-1] + "\n"), (rc, out))
        rc, out = self.run_cmd("export", "--type", "egress.*", "--format", "ocsf")
        event = json.loads(out)
        self.assertEqual((0, 4002, "1.9.0"), (rc, event["class_uid"], event["metadata"]["version"]))
        self.assertEqual(j.path.read_text().splitlines()[-1], event["raw_data"])

    def test_query_refuses_a_broken_journal(self) -> None:
        j = self.journal()
        j.path.write_text(j.path.read_text().replace("blocked", "allowed"))
        self.assertEqual(self.run_cmd("query")[0], 1)


if __name__ == "__main__":
    unittest.main()
