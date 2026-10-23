"""Unit: `bot-bottle audit`."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands.audit import cmd_audit
from bot_bottle.contract.audit import AuditEvent, canonical
from bot_bottle.control.hostctl.audit_journal import AuditJournal


class AuditCommand(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "audit"
        for name, value in (("host_audit_dir", self.dir),
                            ("host_db_path", Path(tmp.name) / "db" / "bot-bottle.db")):
            patcher = mock.patch(f"bot_bottle.cli.commands.audit.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cmd(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_audit(list(args))
        return rc, out.getvalue()

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

    def test_query_refuses_a_broken_journal(self) -> None:
        j = self.journal()
        j.path.write_text(j.path.read_text().replace("blocked", "allowed"))
        self.assertEqual(self.run_cmd("query")[0], 1)


if __name__ == "__main__":
    unittest.main()
