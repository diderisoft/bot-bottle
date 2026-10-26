"""Unit: snapshots of a running agent's workspace, taken from the console."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle.backend import adoption
from bot_bottle.backend.base import ExecResult
from bot_bottle.control.hostctl import remote, snapshots
from bot_bottle.control.hostctl.audit_events import HostAudit
from bot_bottle.control.hostctl.audit_ocsf import to_ocsf
from bot_bottle.control.hostctl.bottles import BottleService
from bot_bottle.control.hostctl.sessions import SessionRecord
from bot_bottle.control.hostctl.snapshots import SnapshotUnavailable, take
from . import use_bottle_root

_PATCH = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1 +1,2 @@\n a\n+b\n"
_GIT = SimpleNamespace(enabled=True, has_host_git_dir=True, guest_path="/home/node/workspace")


def _bottle(result: ExecResult) -> Any:
    return mock.Mock(exec=mock.Mock(return_value=result))


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbsnap.")))
        self.addCleanup(use_bottle_root(self.root))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))


class TestTake(_Root):
    def test_keeps_the_patch_and_its_record(self) -> None:
        record = take(_bottle(ExecResult(0, _PATCH, "")), _GIT, session="s1", activation="a1",
                      seq=12, clock=lambda: 1_800_000_000.25)
        self.assertEqual({"id": "20270115T080000Z-250", "taken": 1_800_000_000.25, "seq": 12,
                          "files": 1, "added": 1, "removed": 0, "bytes": len(_PATCH),
                          "sha256": hashlib.sha256(_PATCH.encode()).hexdigest()}, record)
        run = snapshots.snapshots_root() / "s1" / "a1"
        self.assertEqual(_PATCH, (run / f"{record['id']}.patch").read_text())
        self.assertEqual(record, json.loads((run / f"{record['id']}.json").read_text()))
        self.assertEqual(0o600, (run / f"{record['id']}.patch").stat().st_mode & 0o777)
        self.assertEqual(0o700, snapshots.snapshots_root().stat().st_mode & 0o777)

    def test_only_a_git_workspace(self) -> None:
        with self.assertRaisesRegex(SnapshotUnavailable, "git checkout"):
            take(_bottle(ExecResult(0, "", "")), SimpleNamespace(enabled=True,
                 has_host_git_dir=False, guest_path="/w"), session="s", activation="a", seq=None)

    def test_an_unreadable_workspace(self) -> None:
        with self.assertRaisesRegex(SnapshotUnavailable, "couldn't read the workspace: boom"):
            take(_bottle(ExecResult(1, "", "boom")), _GIT, session="s", activation="a", seq=None)


class TestService(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.sessions = mock.MagicMock()
        self.sessions.record.return_value = SessionRecord(
            id="s1", kind="bottle", meta={"backend": "fake", "activation": "a1"})
        self.sessions.exists.return_value = True
        self.backend = mock.MagicMock()
        self.backend.attach_adopted.return_value = _bottle(ExecResult(0, _PATCH, ""))
        self.audit = mock.MagicMock(spec=HostAudit)
        self.service = BottleService(self.sessions, backend_for=lambda _s: self.backend,
                                     recorded_infra=lambda _b: None, audit=self.audit)
        adoption.write("s1", adoption.AdoptionRecord(
            container="c", registry_id="", workspace=adoption.AdoptedWorkspace(
                enabled=True, has_host_git_dir=True, guest_path="/home/node/workspace")))

    def test_snapshots_and_audits(self) -> None:
        taken = self.service.snapshot("s1", seq=4)
        self.assertEqual((4, 1), (taken["seq"], taken["files"]))
        self.audit.snapshot_taken.assert_called_once_with(bottle="s1", activation="a1",
                                                          snapshot=taken)

    def test_not_running_or_unreachable(self) -> None:
        self.backend.attach_adopted.return_value = None
        with self.assertRaisesRegex(SnapshotUnavailable, "can't reach"):
            self.service.snapshot("s1")
        self.sessions.exists.return_value = False
        with self.assertRaisesRegex(SnapshotUnavailable, "isn't running"):
            self.service.snapshot("s1")


class TestOnTheLink(_Root):
    def link(self, bottles: Any) -> tuple[remote.ConsoleLink, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        return remote.ConsoleLink(bottles, send, agents=lambda: []), sent

    def test_snapshot_request(self) -> None:
        bottles = mock.MagicMock()
        bottles.snapshot.return_value = {"id": "x"}
        link, sent = self.link(bottles)
        link.running["c-1"] = "s1"
        asyncio.run(link.handle({"type": "snapshot", "agent_id": "c-1", "request_id": "r"}))
        self.assertEqual({"type": "snapshot_taken", "request_id": "r", "agent_id": "c-1",
                          "snapshot": {"id": "x"}}, sent[0])
        bottles.snapshot.assert_called_once_with("s1", seq=None)

    def test_errors_go_back_to_the_console(self) -> None:
        bottles = mock.MagicMock()
        link, sent = self.link(bottles)
        asyncio.run(link.handle({"type": "snapshot", "agent_id": "c-9"}))
        self.assertIn("isn't running", sent[0]["error"])
        link.running["c-1"] = "s1"
        bottles.snapshot.side_effect = SnapshotUnavailable("no git")
        asyncio.run(link.handle({"type": "snapshot", "agent_id": "c-1"}))
        self.assertEqual("no git", sent[1]["error"])
        bottles.snapshot.side_effect = RuntimeError("disk")
        asyncio.run(link.handle({"type": "snapshot", "agent_id": "c-1"}))
        self.assertIn("the snapshot failed", sent[2]["error"])
        link, sent = self.link(mock.Mock(spec=["launch"]))  # no snapshot()
        link.running["c-1"] = "s1"
        asyncio.run(link.handle({"type": "snapshot", "agent_id": "c-1"}))
        self.assertIn("can't take snapshots", sent[0]["error"])


class TestAuditEvent(unittest.TestCase):
    def test_snapshot_taken(self) -> None:
        records: list[Any] = []
        audit = HostAudit(None)
        audit.record = records.append  # type: ignore[method-assign]
        audit.snapshot_taken(bottle="b", activation="a", snapshot={"id": "x", "seq": 3,
                                                                   "sha256": "ff", "extra": 1})
        event = records[0]
        self.assertEqual("b/a/x", event.resource)
        self.assertNotIn("extra", cast(dict[str, Any], event.payload))
        self.assertEqual((1008, 4), (to_ocsf(event.to_json())["class_uid"],
                                     to_ocsf(event.to_json())["activity_id"]))


if __name__ == "__main__":
    unittest.main()
