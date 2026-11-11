"""A new label for a bottled agent (`relabel`, the console's `rename`,
`bot-bottle rename`): its `bottled_agents` row changes, its identity doesn't.
The label is kept there only: its session record has none, and `bot-bottle
rename` changes it through the host controller."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.unit import as_cli, serve_host_controller, use_bottle_root
from bot_bottle.bottle_state import (
    MAX_LABEL_CHARS, BottleMetadata, clean_label, read_metadata, rename_label, write_metadata,
)
from bot_bottle.control.hostctl import relabel, remote_actions
from bot_bottle.control.hostctl.sessions import SessionRecord, SessionStore
from bot_bottle.paths import host_db_path


def _meta(identity: str = "fix-it", label: str = "fix it") -> BottleMetadata:
    return BottleMetadata(identity=identity, agent_name="claude", cwd="/w", copy_cwd=False,
                          started_at="2026-11-02T12:00:00+00:00", label=label)


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(  # pylint: disable=consider-using-with
            tempfile.TemporaryDirectory(prefix="relabel.")))
        self.addCleanup(use_bottle_root(root))


class TestCleanLabel(unittest.TestCase):
    def test_trimmed(self) -> None:
        self.assertEqual("look at these", clean_label("  look at these \n"))

    def test_refused(self) -> None:
        for bad, why in ((None, "text"), ("   ", "empty"), ("x" * (MAX_LABEL_CHARS + 1), "at most"),
                         ("a\x00b", "control"), ("a\tb", "control"), ("a\x7fb", "control")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, why):
                clean_label(bad)

    def test_the_longest_is_fine(self) -> None:
        self.assertEqual("x" * MAX_LABEL_CHARS, clean_label("x" * MAX_LABEL_CHARS))


class TestRenameLabel(_Root):
    def test_the_row_takes_the_label_and_keeps_the_identity(self) -> None:
        write_metadata(_meta())
        self.assertTrue(rename_label("fix-it", "the flaky test"))
        meta = read_metadata("fix-it")
        assert meta is not None
        self.assertEqual(("fix-it", "the flaky test", "claude"),
                         (meta.identity, meta.label, meta.agent_name))

    def test_no_row(self) -> None:
        self.assertFalse(rename_label("nobody", "x"))


class TestRename(_Root):
    def test_the_label_is_kept_once(self) -> None:
        write_metadata(_meta())
        SessionStore(host_db_path()).put(SessionRecord("fix-it", "bottle", {"agent": "claude"}))
        self.assertEqual({"label": "new name"}, relabel.rename("fix-it", "  new name "))
        meta = read_metadata("fix-it")
        assert meta is not None
        self.assertEqual("new name", meta.label)
        record = SessionStore(host_db_path()).get("fix-it")
        assert record is not None
        self.assertNotIn("label", record.meta)

    def test_nothing_records_it(self) -> None:
        self.assertEqual({"error": "this host has no bottle ghost"},
                         relabel.rename("ghost", "x"))

    def test_a_bad_label_changes_nothing(self) -> None:
        write_metadata(_meta())
        self.assertIn("error", relabel.rename("fix-it", " "))
        meta = read_metadata("fix-it")
        assert meta is not None
        self.assertEqual("fix it", meta.label)


class _Bottles:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def rename(self, identity: str, label: object) -> dict[str, object]:
        self.calls.append((identity, label))
        return {"label": str(label)}


def _ask(msg: dict[str, Any], running: dict[str, str], bottles: Any) -> dict[str, Any]:
    return asyncio.run(remote_actions.rename(msg, running, bottles))


class TestRenameRequest(unittest.TestCase):
    def test_a_running_agent_is_its_link_bottle(self) -> None:
        bottles = _Bottles()
        reply = _ask({"type": "rename", "request_id": "r1", "agent_id": "a1",
                      "bottle": "someone-else", "label": "new"}, {"a1": "fix-it"}, bottles)
        self.assertEqual([("fix-it", "new")], bottles.calls)
        self.assertEqual({"type": "renamed", "request_id": "r1", "agent_id": "a1",
                          "label": "new"}, reply)

    def test_a_stopped_agent_names_its_bottle(self) -> None:
        bottles = _Bottles()
        _ask({"agent_id": "a1", "bottle": "fix-it", "label": "new"}, {}, bottles)
        self.assertEqual([("fix-it", "new")], bottles.calls)

    def test_refusals(self) -> None:
        bottles = _Bottles()
        for bottle in (None, "", "../etc", "UPPER", 7):
            with self.subTest(bottle=bottle):
                reply = _ask({"agent_id": "a1", "bottle": bottle, "label": "x"}, {}, bottles)
                self.assertEqual("name the bottle to rename", reply["error"])
        self.assertEqual([], bottles.calls)
        reply = _ask({"agent_id": "a1", "bottle": "fix-it", "label": "x"}, {}, object())
        self.assertEqual("this host controller can't rename bottles", reply["error"])

    def test_it_is_a_console_query(self) -> None:
        self.assertIs(remote_actions.QUERIES["rename"], remote_actions.rename)


class TestRenameCommand(_Root):
    def setUp(self) -> None:
        super().setUp()
        serve_host_controller(self)
        as_cli(self)

    def test_renames_through_the_host_controller(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.rename import cmd_rename
        write_metadata(_meta())
        with mock.patch("bot_bottle.cli.commands.rename.info") as said:
            self.assertEqual(0, cmd_rename(["fix-it", "a better name"]))
        meta = read_metadata("fix-it")
        assert meta is not None
        self.assertEqual("a better name", meta.label)
        self.assertIn("'a better name'", said.call_args[0][0])

    def test_an_unknown_bottle_dies(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.rename import cmd_rename
        with mock.patch("bot_bottle.cli.commands.rename.die", side_effect=SystemExit(1)) as die:
            with self.assertRaises(SystemExit):
                cmd_rename(["ghost", "x"])
        self.assertIn("no bottle ghost", die.call_args[0][0])

    def test_dispatch_knows_it(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands import COMMANDS
        self.assertIn("rename", COMMANDS)


if __name__ == "__main__":
    unittest.main()
