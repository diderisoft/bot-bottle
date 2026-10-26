"""Telling an agent its supervise request was answered after it stopped waiting."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from bot_bottle.control.hostctl import nudge

NOW = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)


def _request(age: float, session: str = "s1") -> SimpleNamespace:
    return SimpleNamespace(session=session, tool="add-repo",
                           arrived=(NOW - timedelta(seconds=age)).isoformat())


class TestNudge(unittest.TestCase):
    def test_only_after_the_supervise_wait(self) -> None:
        self.assertFalse(nudge.waited_out(_request(10).arrived, NOW))
        self.assertTrue(nudge.waited_out(_request(31).arrived, NOW))
        self.assertTrue(nudge.waited_out("2026-10-26T19:00:00", NOW))  # naive: UTC
        self.assertFalse(nudge.waited_out("not a time", NOW))

    def test_text_is_one_line(self) -> None:
        self.assertEqual("bot-bottle: your add-repo request was approved by the operator: "
                         "granted a b. Carry on.",
                         nudge.nudge_text("add-repo", "approved", "granted\na  b"))
        self.assertEqual("bot-bottle: your egress-allow request was denied by the operator. "
                         "Carry on.", nudge.nudge_text("egress-allow", "denied"))

    def test_types_the_line_then_enter(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch("time.sleep"):
            self.assertTrue(nudge.type_line("s1", "hello"))
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertIn("has-session", argvs[0])
        self.assertEqual(["send-keys", "-t", "=s1:", "-l", "hello"], argvs[1][-5:])
        self.assertEqual(["send-keys", "-t", "=s1:", "Enter"], argvs[2][-4:])
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(nudge.type_line("gone", "hello"))

    def test_nudges_only_a_request_the_agent_gave_up_on(self) -> None:
        def old(arrived: str) -> bool:
            return arrived == "old"

        with mock.patch.object(nudge, "waited_out", side_effect=old), \
                mock.patch.object(nudge, "type_line", return_value=True) as typed:
            nudge.nudge_if_waited(SimpleNamespace(session="s1", tool="t", arrived="new"),
                                  "approved")
            typed.assert_not_called()
            nudge.nudge_if_waited(SimpleNamespace(session="s1", tool="t", arrived="old"),
                                  "approved", "ok")
        typed.assert_called_once_with("s1", nudge.nudge_text("t", "approved", "ok"))

    def test_never_raises(self) -> None:
        with mock.patch.object(nudge, "waited_out", return_value=True), \
                mock.patch.object(nudge, "type_line", side_effect=OSError("no tmux")):
            nudge.nudge_if_waited(SimpleNamespace(session="s1", tool="t", arrived="x"), "denied")
        with mock.patch.object(nudge, "waited_out", return_value=True), \
                mock.patch.object(nudge, "type_line", return_value=False):
            nudge.nudge_if_waited(SimpleNamespace(session="s1", tool="t", arrived="x"), "denied")


class TestAnswersNudge(unittest.TestCase):
    def test_terminal_and_phone_answers_nudge(self) -> None:
        from bot_bottle.approvals import (  # pylint: disable=import-outside-toplevel
            ALLOW_ONCE, DENY, Request, answer)
        request = Request(id="p1", bottle="b", session="s1", tool="egress-block",
                          justification="", proposed="", current="", arrived="t")
        with mock.patch.object(nudge, "nudge_if_waited") as nudged:
            answer(mock.Mock(), request, DENY, via="the phone")
            answer(mock.Mock(), request, ALLOW_ONCE, via="the terminal")
        self.assertEqual([(request, "denied"), (request, "approved", "allowed once")],
                         [c.args for c in nudged.call_args_list])


if __name__ == "__main__":
    unittest.main()
