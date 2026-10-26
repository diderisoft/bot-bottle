"""Unit: the host end of the console link over iroh (ADR 0007)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import remote as remote_cmd
from bot_bottle.cli.commands.login import _save_credentials
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.control.client import AlreadyAnswered
from bot_bottle.control.hostctl import console_terminal, remote
from bot_bottle.lib.log import Die
from tests.unit import use_bottle_root

try:
    import iroh  # pyright: ignore[reportMissingImports]  # the optional `remote` extra
except ImportError:  # the optional `remote` extra
    iroh = None  # type: ignore[assignment]


class _Bottles:
    """A stand-in for BottleService."""

    def __init__(self, *, launch_error: BaseException | None = None) -> None:
        self.launched: list[tuple[LaunchRequest, str]] = []
        self.torn_down: list[str] = []
        self.running: set[str] = set()
        # GET /bottles entries for sessions started elsewhere.
        self.listed: list[dict[str, object]] = []
        self._error = launch_error

    def launch(self, request: LaunchRequest, role: str = "cli") -> dict[str, object]:
        if self._error is not None:
            raise self._error
        self.launched.append((request, role))
        self.running.add("bottle-1")
        return {"bottle": "bottle-1", "session": "bottle-1"}

    def teardown(self, bottle_id: str, role: str = "cli") -> dict[str, object]:
        del role
        self.torn_down.append(bottle_id)
        self.running.discard(bottle_id)
        return {"bottle": bottle_id, "exit": {"status": 0, "signal": ""}}

    def session_state(self, session_id: str) -> dict[str, object]:
        if session_id in self.running:
            return {"running": True, "exit": None}
        return {"running": False, "exit": {"status": 3, "signal": ""}}

    def list(self) -> list[dict[str, object]]:
        return [{**entry, "session": self.session_state(str(entry["id"]))}
                for entry in self.listed]

    def start_elsewhere(self, bottle: str, *, origin: str = "terminal",
                        activation: str = "act-t") -> None:
        """A session the link didn't launch, as `bot-bottle` starts one."""
        self.running.add(bottle)
        self.listed.append({"id": bottle, "agent": "implementer", "label": bottle,
                            "activation": activation, "origin": origin, "writable": True})


def _link(bottles: _Bottles) -> tuple[remote.ConsoleLink, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    return remote.ConsoleLink(bottles, send, agents=lambda: ["implementer", "reviewer"]), sent


class TestConsoleLink(unittest.TestCase):
    def test_lists_local_agents(self) -> None:
        link, sent = _link(_Bottles())
        asyncio.run(link.handle({"type": "list_local", "request_id": "r1"}))
        self.assertEqual([{"type": "local_definitions", "request_id": "r1",
                           "definitions": [{"name": "implementer"}, {"name": "reviewer"}]}],
                         sent)

    def test_launches_a_local_agent_by_name(self) -> None:
        bottles = _Bottles()
        link, sent = _link(bottles)
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                 "definition": {"name": "implementer"}}))
        self.assertEqual(1, len(bottles.launched))
        request, role = bottles.launched[0]
        self.assertEqual(("implementer", True, "remote"),
                         (request.agent, request.assume_yes, role))
        # No terminal relay in this link, so the console can't type into it.
        self.assertEqual([{"type": "agent_started", "agent_id": "a1", "bottle": "bottle-1",
                           "writable": False, "transcript": False}], sent)

    def test_refuses_console_supplied_agent_content(self) -> None:
        # Agents and the bottles they pick come from this host (ADR 0007).
        bottles = _Bottles()
        link, sent = _link(bottles)
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1", "definition": {
            "name": "implementer", "content": "---\nbottle: wide-open\n---\n"}}))
        self.assertEqual([], bottles.launched)
        self.assertEqual("agent_errored", sent[0]["type"])

    def test_refuses_an_unknown_agent(self) -> None:
        bottles = _Bottles()
        link, sent = _link(bottles)
        for definition in ({"name": "nope"}, {}, None):
            asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                     "definition": definition}))
        self.assertEqual([], bottles.launched)
        self.assertEqual({"agent_errored"}, {m["type"] for m in sent})

    def test_a_failed_launch_is_reported(self) -> None:
        link, sent = _link(_Bottles(launch_error=Die(1, "no backend available")))
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                 "definition": {"name": "implementer"}}))
        self.assertEqual("agent_errored", sent[0]["type"])
        self.assertIn("no backend available", sent[0]["message"])

    def test_stop_tears_down_and_reports(self) -> None:
        bottles = _Bottles()
        link, sent = _link(bottles)
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                 "definition": {"name": "implementer"}}))
        asyncio.run(link.handle({"type": "stop", "agent_id": "a1"}))
        self.assertEqual(["bottle-1"], bottles.torn_down)
        self.assertEqual({"type": "agent_stopped", "agent_id": "a1", "exit_code": 0}, sent[-1])

    def test_stop_ignores_agents_it_did_not_launch(self) -> None:
        bottles = _Bottles()
        link, sent = _link(bottles)
        asyncio.run(link.handle({"type": "stop", "agent_id": "someone-elses"}))
        self.assertEqual(([], []), (bottles.torn_down, sent))

    def test_an_agent_that_exits_is_reported_stopped(self) -> None:
        bottles = _Bottles()
        link, sent = _link(bottles)
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                 "definition": {"name": "implementer"}}))
        bottles.running.clear()  # the agent exited on its own
        asyncio.run(link.reap())
        self.assertEqual({"type": "agent_stopped", "agent_id": "a1", "exit_code": 3}, sent[-1])
        self.assertEqual({}, link.running)


_CURRENT = 'routes:\n  - host: "github.com"\n'
_PROPOSED = _CURRENT + '  - host: "api.stripe.com"\n    inspect:\n      matches:\n' \
    '        - methods: ["GET"]\n'


def _pending(pid: str = "p1", session: str = "bottle-1") -> dict[str, Any]:
    return {"id": pid, "bottle_slug": "b-9", "bottle_label": session, "tool": "egress-allow",
            "justification": "schema", "proposed_file": _PROPOSED, "current_file": _CURRENT,
            "arrival_timestamp": "2026-10-23T18:00:01.123456+00:00"}


class TestApprovals(unittest.TestCase):
    """Pending requests raised as the console's `attention`, answered by `approval`."""

    def link(self, pending: list[dict[str, Any]]) -> tuple[remote.ConsoleLink, list[dict[str, Any]],
                                                           mock.MagicMock]:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        client = mock.MagicMock()

        def respond(pid: str, **_kwargs: object) -> None:
            pending[:] = [p for p in pending if p["id"] != pid]

        client.supervise_respond.side_effect = respond
        link = remote.ConsoleLink(_Bottles(), send, agents=lambda: ["implementer"],
                                  requests=lambda: list(pending), orchestrator=lambda _url: client)
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                 "definition": {"name": "implementer"}}))
        return link, sent, client

    def test_a_request_is_raised_once_and_cleared_with_activity(self) -> None:
        pending = [_pending()]
        link, sent, _ = self.link(pending)
        asyncio.run(link.push_attention())
        asyncio.run(link.push_attention())  # unchanged: not raised again
        raised = [m for m in sent if m["type"] == "attention"]
        self.assertEqual(1, len(raised))
        self.assertEqual(("a1", "approval", "2026-10-23T18:00:01.123456+00:00"),
                         (raised[0]["agent_id"], raised[0]["reason"], raised[0]["since"]))
        self.assertIn("bottle-1 wants GET api.stripe.com", raised[0]["message"])
        self.assertIn("api.stripe.com  GET", raised[0]["message"])
        pending.clear()
        asyncio.run(link.push_attention())
        self.assertEqual({"type": "activity", "agent_id": "a1"}, sent[-1])

    def test_a_forge_request_raises_the_connect_card(self) -> None:
        pending = [{**_pending(), "tool": "forge-connect",
                    "proposed_file": "https://gitea.example.com"}]
        link, sent, _ = self.link(pending)
        with mock.patch("bot_bottle.control.hostctl.remote_actions._connected_hosts",
                        return_value=set()):
            asyncio.run(link.push_attention())
        raised = [m for m in sent if m["type"] == "attention"][-1]
        self.assertEqual(["forge_connect"], raised["options"])
        self.assertIn("forge: https://gitea.example.com", raised["message"])
        asyncio.run(link.handle({"type": "forge_connect_answer", "request_id": "q",
                                 "agent_id": "a1", "since": "2000-01-01T00:00:00", "token": "t"}))
        self.assertEqual("forge_connect_answered", sent[-1]["type"])
        self.assertIn("no longer waiting", sent[-1]["error"])

    def test_approval_answers_the_raised_request(self) -> None:
        pending = [_pending()]
        link, sent, client = self.link(pending)
        asyncio.run(link.push_attention())
        # The console echoes since after a round trip through its database.
        asyncio.run(link.handle({"type": "approval", "agent_id": "a1", "decision": "deny",
                                 "since": "2026-10-23T18:00:01.123456"}))
        self.assertEqual("reject", client.supervise_respond.call_args.kwargs["decision"])
        self.assertIn("the phone", client.supervise_respond.call_args.kwargs["notes"])
        self.assertEqual({"type": "activity", "agent_id": "a1"}, sent[-1])

    def test_an_answer_from_the_terminal_first_clears_the_phone(self) -> None:
        pending = [_pending()]
        link, sent, client = self.link(pending)
        asyncio.run(link.push_attention())

        def answered_in_the_terminal(pid: str, **_kwargs: object) -> None:
            pending.clear()
            raise AlreadyAnswered("approved")

        client.supervise_respond.side_effect = answered_in_the_terminal
        asyncio.run(link.handle({"type": "approval", "agent_id": "a1", "decision": "deny",
                                 "since": "2026-10-23T18:00:01.123456"}))
        self.assertEqual({"type": "activity", "agent_id": "a1"}, sent[-1])

    def test_an_answer_goes_to_the_orchestrator_holding_the_request(self) -> None:
        pending = [{**_pending(), "orchestrator_url": "http://10.243.255.1:8099"}]
        link, _sent, client = self.link(pending)
        asked: list[str] = []
        def orchestrator(url: str) -> mock.MagicMock:
            asked.append(url)
            return client
        link._orchestrator = orchestrator  # pylint: disable=protected-access
        asyncio.run(link.push_attention())
        asyncio.run(link.handle({"type": "approval", "agent_id": "a1", "decision": "allow",
                                 "since": "2026-10-23T18:00:01.123456"}))
        self.assertEqual(["http://10.243.255.1:8099"], asked)
        self.assertEqual("approve", client.supervise_respond.call_args.kwargs["decision"])

    def test_a_stale_or_unknown_approval_is_ignored(self) -> None:
        link, _, client = self.link([_pending()])
        asyncio.run(link.push_attention())
        for msg in ({"agent_id": "a1", "decision": "allow", "since": "2026-10-23T17:00:00"},
                    {"agent_id": "a1", "decision": "maybe", "since": "2026-10-23T18:00:01"},
                    {"agent_id": "nope", "decision": "allow", "since": "2026-10-23T18:00:01"}):
            with self.subTest(msg=msg):
                asyncio.run(link.handle({"type": "approval", **msg}))
        client.supervise_respond.assert_not_called()

    def test_requests_from_bottles_this_link_did_not_launch_are_not_raised(self) -> None:
        link, sent, _ = self.link([_pending(session="someone-elses")])
        asyncio.run(link.push_attention())
        self.assertEqual([], [m for m in sent if m["type"] == "attention"])


class _RootCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="remote-root."))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.root))


class _Terminal:
    """A stand-in for ConsoleTerminal."""

    def __init__(self, writable: bool) -> None:
        self.writable = writable
        self.written: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.history_asked: list[int] = []
        self.closed = False

    def write(self, data: bytes) -> bool:
        if not self.writable:
            return False
        self.written.append(data)
        return True

    def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    def history(self, lines: int) -> tuple[str, int, int]:
        self.history_asked.append(lines)
        return "older\nnewer\n", 2, 2000

    def close(self) -> None:
        self.closed = True


class _ActivationBottles(_Bottles):
    def launch(self, request: LaunchRequest, role: str = "cli") -> dict[str, object]:
        result = super().launch(request, role)
        return {**result, "activation": "act-1"}


class TestConsoleSession(unittest.TestCase):
    """Prompt-first launches, the terminal relay's input/resize, and the
    audit journal query (the console's sidebar-first layout PRD)."""

    def _link(self, *, writable: bool = True, audit: Any = None,
              state: "remote.LinkState | None" = None,
              bottles: "_ActivationBottles | None" = None,
              ) -> tuple[remote.ConsoleLink, _ActivationBottles, list[dict[str, Any]]]:
        bottles = bottles if bottles is not None else _ActivationBottles()
        sent: list[dict[str, Any]] = []
        if not hasattr(self, "terms"):
            self.terms: dict[str, _Terminal] = {}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def terminals(agent_id: str, bottle: str) -> _Terminal:
            self.terms[agent_id] = _Terminal(writable)
            return self.terms[agent_id]

        link = remote.ConsoleLink(bottles, send, agents=lambda: ["implementer"],
                                  terminals=terminals, audit=audit, state=state)
        return link, bottles, sent

    def test_a_reconnected_link_resumes_its_agents(self) -> None:
        state = remote.LinkState()
        link, bottles, _sent = self._link(state=state)
        self._launch(link)
        link.close()  # the connection dropped
        self.assertTrue(self.terms["a1"].closed)
        again, _bottles, sent = self._link(state=state, bottles=bottles)
        asyncio.run(again.resume())
        self.assertFalse(self.terms["a1"].closed)  # the relay is open again
        self.assertEqual({"type": "agents_running", "agent_ids": ["a1"]}, sent[-1])
        # Stop still reaches the agent launched on the old connection.
        asyncio.run(again.handle({"type": "stop", "agent_id": "a1"}))
        self.assertEqual("agent_stopped", sent[-1]["type"])

    def test_resume_relaunches_a_stopped_bottle_under_its_identity(self) -> None:
        from bot_bottle.bottle_state import BottleMetadata, write_metadata  # pylint: disable=import-outside-toplevel
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))
        write_metadata(BottleMetadata(identity="old-run", agent_name="implementer", cwd="/w",
                                      copy_cwd=True, started_at="", label="old run",
                                      bottle_names=("implementer", "basic")))
        link, bottles, sent = self._link()
        asyncio.run(link.handle({"type": "resume", "agent_id": "a9", "bottle": "old-run"}))
        request, role = bottles.launched[-1]
        self.assertEqual(("implementer", ("implementer", "basic"), "old-run", "old run", "/w",
                          True, "a9", remote.ROLE_REMOTE),
                         (request.agent, request.bottles, request.identity, request.label,
                          request.cwd, request.copy_cwd, request.agent_id, role))
        self.assertEqual("agent_started", sent[-1]["type"])
        self.assertEqual("a9", sent[-1]["agent_id"])

    def test_resume_is_refused_for_what_cant_be_resumed(self) -> None:
        from bot_bottle.bottle_state import BottleMetadata, write_metadata  # pylint: disable=import-outside-toplevel
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))
        write_metadata(BottleMetadata(identity="gone-agent", agent_name="deleted", cwd="",
                                      copy_cwd=False, started_at=""))
        write_metadata(BottleMetadata(identity="busy", agent_name="implementer", cwd="",
                                      copy_cwd=False, started_at=""))
        link, bottles, sent = self._link()
        bottles.running.add("busy")
        for bottle, why in (("../x", "the bottle it ran as"), ("nothing-saved", "no saved state"),
                            ("gone-agent", "no longer has the agent"),
                            ("busy", "still running")):
            with self.subTest(bottle=bottle):
                asyncio.run(link.handle({"type": "resume", "agent_id": "a9", "bottle": bottle}))
                self.assertEqual("agent_errored", sent[-1]["type"])
                self.assertIn(why, sent[-1]["message"])
        self.assertEqual([], bottles.launched)

    def test_refresh_credentials_answers_with_the_outcome(self) -> None:
        link, bottles, sent = self._link()
        asyncio.run(link.handle({"type": "refresh_credentials", "agent_id": "a1",
                                 "request_id": "q1"}))
        self.assertEqual({"type": "credentials_refreshed", "request_id": "q1", "agent_id": "a1",
                          "error": "that agent isn't running on this host"}, sent[-1])
        self._launch(link)
        asyncio.run(link.handle({"type": "refresh_credentials", "agent_id": "a1",
                                 "request_id": "q2"}))
        self.assertIn("can't refresh", str(sent[-1]["error"]))
        bottles.refresh_credentials = lambda bottle: {  # type: ignore[attr-defined]
            "refreshed": True, "expires_at": "x", "bottle": bottle}
        asyncio.run(link.handle({"type": "refresh_credentials", "agent_id": "a1",
                                 "request_id": "q3"}))
        self.assertEqual(("q3", True, "bottle-1"), (sent[-1]["request_id"], sent[-1]["refreshed"],
                                                    sent[-1]["bottle"]))

    def test_a_relay_that_ended_is_reopened(self) -> None:
        # A broken console stream ends a relay; typing, the watch and a
        # reconnect each bring it back rather than leaving the agent mute.
        link, _bottles, _sent = self._link()
        self._launch(link)
        dead = self.terms["a1"]
        dead.closed = True
        asyncio.run(link.handle({"type": "input", "agent_id": "a1", "data": "hello?"}))
        self.assertIsNot(dead, self.terms["a1"])
        self.assertEqual([b"hello?"], self.terms["a1"].written)
        self.terms["a1"].closed = True
        asyncio.run(link.reap())
        self.assertFalse(self.terms["a1"].closed)
        self.terms["a1"].closed = True
        asyncio.run(link.resume())
        self.assertFalse(self.terms["a1"].closed)

    def test_an_agent_that_exited_while_away_is_reported(self) -> None:
        state = remote.LinkState()
        link, bottles, _sent = self._link(state=state)
        self._launch(link)
        link.close()
        bottles.running.clear()  # the agent exited meanwhile
        again, _bottles, sent = self._link(state=state, bottles=bottles)
        asyncio.run(again.resume())
        self.assertEqual([("agent_stopped", "a1"), ("agents_running", None)],
                         [(m["type"], m.get("agent_id")) for m in sent])
        self.assertEqual([], sent[-1]["agent_ids"])

    def test_hello_is_quiet(self) -> None:
        link, _bottles, sent = self._link()
        asyncio.run(link.handle({"type": "hello"}))
        self.assertEqual([], sent)

    def _launch(self, link: remote.ConsoleLink, **extra: Any) -> None:
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                 "definition": {"name": "implementer"}, **extra}))

    def test_prompt_bottles_and_label_reach_the_launch(self) -> None:
        link, bottles, sent = self._link()
        self._launch(link, prompt="Fix the flaky test", bottles=["python-dev"], label="fix-it")
        request, _role = bottles.launched[0]
        self.assertEqual(("Fix the flaky test", ("python-dev",), "fix-it", False),
                         (request.prompt, request.bottles, request.label, request.headless))
        self.assertEqual({"type": "agent_started", "agent_id": "a1", "bottle": "bottle-1",
                          "activation": "act-1", "writable": True, "transcript": False}, sent[-1])

    def test_bad_launch_extras_are_refused(self) -> None:
        for extra in ({"prompt": 5}, {"prompt": "x" * 20_001}, {"bottles": "python-dev"},
                      {"bottles": ["../etc"]}, {"bottles": ["b"] * 9}, {"label": ["x"]}):
            with self.subTest(extra=extra):
                link, bottles, sent = self._link()
                self._launch(link, **extra)
                self.assertEqual([], bottles.launched)
                self.assertEqual("agent_errored", sent[-1]["type"])

    def test_input_types_into_a_writable_session(self) -> None:
        link, _bottles, _sent = self._link()
        self._launch(link)
        asyncio.run(link.handle({"type": "input", "agent_id": "a1", "data": "yes\r"}))
        self.assertEqual([b"yes", b"\r"], self.terms["a1"].written)

    def test_an_enter_waits_for_its_own_keystroke(self) -> None:
        # A message and its Enter arriving together would read as a paste,
        # whose Enter Claude Code takes for a newline.
        link, _bottles, _sent = self._link()
        self._launch(link)
        start = time.monotonic()
        asyncio.run(link.handle({"type": "input", "agent_id": "a1", "data": "hi"}))
        asyncio.run(link.handle({"type": "input", "agent_id": "a1", "data": "\r"}))
        self.assertGreaterEqual(time.monotonic() - start, console_terminal.SUBMIT_GAP_SECONDS * 0.9)
        self.assertEqual([b"hi", b"\r"], self.terms["a1"].written)
        # A lone Enter long after typing goes at once.
        link._typed["a1"] -= 10  # pylint: disable=protected-access
        start = time.monotonic()
        asyncio.run(link.handle({"type": "input", "agent_id": "a1", "data": "\r"}))
        self.assertLess(time.monotonic() - start, console_terminal.SUBMIT_GAP_SECONDS)

    def test_input_is_dropped_when_the_agent_disallows_it(self) -> None:
        link, _bottles, sent = self._link(writable=False)
        self._launch(link)
        self.assertFalse(sent[-1]["writable"])
        asyncio.run(link.handle({"type": "input", "agent_id": "a1", "data": "rm -rf /\r"}))
        self.assertEqual([], self.terms["a1"].written)

    def test_input_for_unknown_agents_and_non_text_is_ignored(self) -> None:
        link, _bottles, _sent = self._link()
        self._launch(link)
        asyncio.run(link.handle({"type": "input", "agent_id": "other", "data": "x"}))
        asyncio.run(link.handle({"type": "input", "agent_id": "a1", "data": ["x"]}))
        self.assertEqual([], self.terms["a1"].written)

    def test_resize_is_validated(self) -> None:
        link, _bottles, _sent = self._link()
        self._launch(link)
        for cols, rows in ((46, 30), (5, 30), ("80", 24), (True, 24), (80, 999)):
            asyncio.run(link.handle({"type": "resize", "agent_id": "a1", "cols": cols, "rows": rows}))
        self.assertEqual([(46, 30)], self.terms["a1"].sizes)

    def test_history_comes_from_the_relay(self) -> None:
        link, _bottles, sent = self._link()
        self._launch(link)
        asyncio.run(link.handle({"type": "history", "agent_id": "a1", "request_id": "h1",
                                 "lines": 200}))
        self.assertEqual({"type": "history", "request_id": "h1", "text": "older\nnewer\n",
                          "size": 2, "limit": 2000}, sent[-1])
        for lines in (None, "5", True, -3):  # anything else asks for the most
            asyncio.run(link.handle({"type": "history", "agent_id": "a1", "lines": lines}))
        self.assertEqual([200, 3000, 3000, 3000, 3000], self.terms["a1"].history_asked)
        asyncio.run(link.handle({"type": "history", "agent_id": "nobody", "request_id": "h2"}))
        self.assertEqual({"type": "history", "request_id": "h2", "text": "", "size": 0,
                          "limit": 0}, sent[-1])

    def test_stop_and_disconnect_close_the_relay(self) -> None:
        link, _bottles, _sent = self._link()
        self._launch(link)
        asyncio.run(link.handle({"type": "stop", "agent_id": "a1"}))
        self.assertTrue(self.terms["a1"].closed)
        self._launch(link)
        link.close()
        self.assertTrue(self.terms["a1"].closed)

    def test_an_agent_that_exits_closes_its_relay(self) -> None:
        link, bottles, _sent = self._link()
        self._launch(link)
        bottles.running.clear()
        asyncio.run(link.reap())
        self.assertTrue(self.terms["a1"].closed)

    def test_audit_query_answers_for_launched_bottles_only(self) -> None:
        calls: list[tuple[str, str | None, int]] = []

        def audit(bottle: str, activation: str | None, limit: int) -> list[dict[str, Any]]:
            calls.append((bottle, activation, limit))
            return [{"id": "e1", "bottle": bottle}]

        link, _bottles, sent = self._link(audit=audit)
        asyncio.run(link.handle({"type": "audit_query", "request_id": "r0", "bottle": "bottle-1"}))
        self.assertEqual({"type": "audit_events", "request_id": "r0", "events": []}, sent[-1])
        self.assertEqual([], calls)  # not launched by this console: nothing, not an error
        self._launch(link)
        asyncio.run(link.handle({"type": "audit_query", "request_id": "r1", "bottle": "bottle-1",
                                 "activation": "act-1", "limit": 10_000}))
        self.assertEqual([("bottle-1", "act-1", 500)], calls)
        self.assertEqual([{"id": "e1", "bottle": "bottle-1"}], sent[-1]["events"])

    def test_audit_query_failure_answers_empty(self) -> None:
        def audit(*_args: Any) -> list[dict[str, Any]]:
            raise RuntimeError("index broke")

        link, _bottles, sent = self._link(audit=audit)
        self._launch(link)
        asyncio.run(link.handle({"type": "audit_query", "request_id": "r1", "bottle": "bottle-1"}))
        self.assertEqual([], sent[-1]["events"])


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TestHistoryRequests(unittest.TestCase):
    """`history` without tmux: the relay's bounds, and an unreadable reply."""

    def test_the_relay_bounds_what_it_asks_for_and_sends(self) -> None:
        from bot_bottle.control.hostctl import console_terminal as ct

        sessions = mock.MagicMock()
        line = "\x1b[31m" + "x" * 90 + "\x1b[0m\n"
        sessions.history.return_value = (line * (ct.MAX_HISTORY_CHARS // len(line) + 3), 9, 2000)
        term = ct.ConsoleTerminal(sessions, "s1", "a1", writable_allowed=False)
        text, size, limit = term.history(10**6)
        sessions.history.assert_called_once_with("s1", ct.HISTORY_LINES)
        self.assertLessEqual(len(text), ct.MAX_HISTORY_CHARS)
        self.assertTrue(text.startswith("\x1b[31m"))  # whole lines only
        self.assertEqual((9, 2000), (size, limit))
        term.history(0)
        self.assertEqual(1, sessions.history.call_args.args[1])
        term.close()
        self.assertEqual(("", 0, 0), term.history(5))

    def test_an_unreadable_tmux_reply_is_empty(self) -> None:
        from bot_bottle.control.hostctl import sessions as sessions_mod

        manager = sessions_mod.SessionManager({})
        for out in ("", "no such session\n", "12\n"):
            with self.subTest(out=out), mock.patch.object(
                    sessions_mod.subprocess, "run",
                    return_value=mock.Mock(stdout=out, returncode=1)):
                self.assertEqual(("", 0, 0), manager.history("gone", 100))


class TestConsoleTerminal(unittest.TestCase):
    """A real relay against a real tmux session running `cat`."""

    def setUp(self) -> None:
        from bot_bottle.control.hostctl import sessions as sessions_mod

        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.addCleanup(use_bottle_root(Path(self._tmp)))
        sessions_mod.write_config()
        self.tmux = sessions_mod.tmux
        import subprocess
        subprocess.run(self.tmux("new-session", "-d", "-s", "s1", "-x", "80", "-y", "24", "cat"),
                       check=True)
        self.addCleanup(subprocess.run, self.tmux("kill-server"), capture_output=True)
        self.manager = sessions_mod.SessionManager({})

    def _run(self, writable: bool) -> bytes:
        from bot_bottle.control.hostctl.console_terminal import ConsoleTerminal

        received = bytearray()

        class _Stream:
            async def write_all(self, data: bytes) -> None:
                received.extend(data)

            async def finish(self) -> None:
                pass

        async def scenario() -> None:
            async def open_stream() -> _Stream:
                return _Stream()

            term = ConsoleTerminal(self.manager, "s1", "agent-9", writable_allowed=writable)
            await term.start(open_stream, cols=80, rows=24)
            await asyncio.sleep(1.0)  # the client attaches (and turns writable)
            term.write(b"hello-from-console\r")
            term.resize(46, 30)
            await asyncio.sleep(1.0)
            term.close()
            await asyncio.sleep(0.5)

        asyncio.run(scenario())
        return bytes(received)

    def test_writable_session_echoes_typing(self) -> None:
        out = self._run(writable=True)
        self.assertTrue(out.startswith(b"agent-9\n"))
        self.assertIn(b"hello-from-console", out)

    def test_read_only_session_drops_typing(self) -> None:
        out = self._run(writable=False)
        self.assertTrue(out.startswith(b"agent-9\n"))
        self.assertNotIn(b"hello-from-console", out)


    def test_history_is_what_scrolled_off_with_colors(self) -> None:
        import subprocess
        _text, size, _limit = self.manager.history("s1", 3000)
        self.assertEqual(0, size)  # nothing has scrolled off yet
        subprocess.run(self.tmux("send-keys", "-t", "=s1:", "-l", "\x1b[31mred\x1b[0m"), check=True)
        subprocess.run(self.tmux("send-keys", "-t", "=s1:", "Enter"), check=True)
        for i in range(60):
            subprocess.run(self.tmux("send-keys", "-t", "=s1:", f"line-{i}", "Enter"), check=True)
        time.sleep(0.5)
        text, size, limit = self.manager.history("s1", 3000)
        self.assertIn("\x1b[31mred", text)  # long off the 24-row screen, colors kept
        self.assertIn("line-0", text)
        self.assertNotIn("line-59", text)  # still on screen: the live terminal shows it
        self.assertEqual(size, len(text.splitlines()))  # the size matches the text
        self.assertGreater(limit, size)
        tail, _size, _limit = self.manager.history("s1", 3)
        self.assertEqual(text.splitlines()[-3:], tail.splitlines())  # just the newest

class TestConfig(_RootCase):
    def test_config_round_trips_privately(self) -> None:
        remote.save_config(remote.RemoteConfig(True, "cid", "ticket"))
        self.assertEqual(remote.RemoteConfig(True, "cid", "ticket"), remote.load_config())
        self.assertEqual(0o600, (self.root / remote.REMOTE_FILE).stat().st_mode & 0o777)

    def test_missing_config_is_disabled(self) -> None:
        self.assertFalse(remote.load_config().enabled)

    def test_the_secret_key_persists_privately(self) -> None:
        key = remote.secret_key()
        self.assertEqual((32, key), (len(key), remote.secret_key()))
        self.assertEqual(0o600, (self.root / remote.KEY_FILE).stat().st_mode & 0o777)

    def test_login_pins_the_console_identity(self) -> None:
        _save_credentials("https://console", "h1", "at", "rt",
                          {"console_node_id": "cid", "console_ticket": "tk"})
        saved = json.loads((self.root / "console.json").read_text())
        self.assertEqual(("cid", "tk"), (saved["console_node_id"], saved["console_ticket"]))


class TestLocalAgents(unittest.TestCase):
    """Remote launches may name only the agents under `~/.bot-bottle/`."""

    def test_lists_the_home_agents(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="remote-home."))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        agents = home / ".bot-bottle" / "agents"
        agents.mkdir(parents=True)
        for name in ("reviewer", "implementer"):
            (agents / f"{name}.md").write_text("---\nbottle: dev\n---\nprompt\n")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(["implementer", "reviewer"], remote.local_agents())

    def test_no_manifest_lists_nothing(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="remote-home."))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual([], remote.local_agents())


class TestRemoteEnable(_RootCase):
    def _login(self, **extra: str) -> None:
        remote.save_console({"url": "https://console", "host_id": "h1",
                             "access_token": "at", "refresh_token": "rt", **extra})

    def _enable(self, responses: list[object]) -> tuple[int, str, list[tuple[Any, ...]]]:
        calls: list[tuple[Any, ...]] = []

        def fake(method: str, url: str, body: dict[str, Any], token: str = "") -> object:
            calls.append((method, url, body, token))
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(remote_cmd, "_request", side_effect=fake), \
                mock.patch.object(remote, "node_id", return_value="hostnode"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = remote_cmd.cmd_remote(["enable"])
        return rc, out.getvalue() + err.getvalue(), calls

    def test_binds_pins_and_enables(self) -> None:
        self._login()
        rc, _, calls = self._enable([{"console_node_id": "cid", "console_ticket": "tk"}])
        self.assertEqual(0, rc)
        self.assertEqual(("PUT", "https://console/api/v1/hosts/iroh",
                          {"node_id": "hostnode"}, "at"), calls[0])
        self.assertEqual(remote.RemoteConfig(True, "cid", "tk"), remote.load_config())
        self.assertEqual("cid", remote.load_console()["console_node_id"])

    def test_refreshes_an_expired_token_once(self) -> None:
        self._login()
        expired = urllib.error.HTTPError("u", 401, "expired", {}, None)  # type: ignore[arg-type]
        rc, _, calls = self._enable([expired, {"access_token": "at2"},
                                     {"console_node_id": "cid", "console_ticket": "tk"}])
        self.assertEqual(0, rc)
        self.assertEqual("at2", calls[-1][3])
        self.assertEqual("at2", remote.load_console()["access_token"])

    def test_refuses_a_console_whose_identity_changed(self) -> None:
        self._login(console_node_id="pinned")
        rc, output, _ = self._enable([{"console_node_id": "other", "console_ticket": "tk"}])
        self.assertEqual(1, rc)
        self.assertIn("changed since login", output)
        self.assertFalse(remote.load_config().enabled)

    def test_a_repaired_machine_takes_its_original_host_id(self) -> None:
        self._login()  # the new pairing's host, h1
        rc, output, _ = self._enable([{"host_id": "h0", "console_node_id": "cid",
                                       "console_ticket": "tk"}])
        self.assertEqual(0, rc)
        self.assertEqual("h0", remote.load_console()["host_id"])
        self.assertIn("already paired", output)

    def test_a_machine_paired_to_another_account_says_so(self) -> None:
        self._login()
        body = io.BytesIO(json.dumps({"detail": "this machine is paired to another account "
                                                "on this console"}).encode())
        refused = urllib.error.HTTPError("u", 409, "conflict", {}, body)  # type: ignore[arg-type]
        rc, output, _ = self._enable([refused])
        self.assertEqual(1, rc)
        self.assertIn("paired to another account", output)
        self.assertFalse(remote.load_config().enabled)

    def test_needs_a_login(self) -> None:
        rc, output, _ = self._enable([])
        self.assertEqual(1, rc)
        self.assertIn("login", output)

    def test_says_how_to_get_the_bindings(self) -> None:
        self._login()
        err = io.StringIO()
        with mock.patch.object(remote, "import_iroh",
                               side_effect=remote.RemoteError("install `bot-bottle[remote]`")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(1, remote_cmd.cmd_remote(["enable"]))
        self.assertIn("bot-bottle[remote]", err.getvalue())


class TestRemoteCommand(_RootCase):
    """The CLI's other paths: the HTTP helper, refusals, disable and status."""

    def _run(self, *argv: str, **patches: Any) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(remote_cmd, name, value))
            stack.enter_context(mock.patch.object(remote, "node_id", return_value="hostnode"))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            rc = remote_cmd.cmd_remote(list(argv))
        return rc, out.getvalue()

    def _login(self) -> None:
        remote.save_console({"url": "https://console", "host_id": "h1",
                             "access_token": "at", "refresh_token": "rt"})

    def test_request_sends_json_with_the_bearer_token(self) -> None:
        sent: dict[str, Any] = {}

        class _Resp(io.BytesIO):
            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc: object) -> None:
                pass

        def urlopen(req: Any, timeout: float) -> _Resp:
            del timeout
            sent.update(method=req.get_method(), url=req.full_url,
                        auth=req.get_header("Authorization"), body=json.loads(req.data))
            return _Resp(b'{"ok": true}')

        request = getattr(remote_cmd, "_request")
        with mock.patch.object(remote_cmd.urllib.request, "urlopen", urlopen):
            self.assertEqual({"ok": True}, request("PUT", "https://c/x", {"a": 1}, "tok"))
        self.assertEqual({"method": "PUT", "url": "https://c/x", "auth": "Bearer tok",
                          "body": {"a": 1}}, sent)

    def test_explains_each_refusal(self) -> None:
        self._login()
        for status, phrase in ((401, "rejected this host's token"), (409, "another host"), (500, "HTTP 500")):
            error = urllib.error.HTTPError("u", status, "no", {}, None)  # type: ignore[arg-type]
            with self.subTest(status=status):
                rc, output = self._run("enable", _bind=mock.Mock(side_effect=error))
                self.assertEqual((1, True), (rc, phrase in output), output)
        rc, output = self._run("enable", _bind=mock.Mock(side_effect=OSError("down")))
        self.assertEqual((1, True), (rc, "couldn't reach the console" in output))
        rc, output = self._run("enable", _bind=mock.Mock(return_value={}))
        self.assertEqual((1, True), (rc, "iroh identity" in output))
        self.assertFalse(remote.load_config().enabled)

    def test_disable_keeps_the_pinned_console(self) -> None:
        remote.save_config(remote.RemoteConfig(True, "cid", "tk"))
        self.assertEqual(0, self._run("disable")[0])
        self.assertEqual(remote.RemoteConfig(False, "cid", "tk"), remote.load_config())

    def test_status_reports_the_link(self) -> None:
        self.assertIn("disabled", self._run("status")[1])
        remote.save_config(remote.RemoteConfig(True, "cid0123456789", "tk"))
        self.assertIn("no report yet", self._run("status")[1])
        remote.write_status(connected=False, error="console unreachable")
        output = self._run("status")[1]
        self.assertIn("not connected (console unreachable)", output)
        remote.write_status(connected=True)
        self.assertIn("link: connected", self._run("status")[1])

    def test_usage(self) -> None:
        self.assertEqual(2, self._run("bogus")[0])
        self.assertEqual(0, self._run("--help")[0])

    def test_a_malformed_status_file_reads_as_no_report(self) -> None:
        (self.root / remote.STATUS_FILE).write_text("[not, an, object")
        self.assertEqual({}, remote.read_status())


class TestSupervisor(_RootCase):
    """The link runs exactly while remote.json says it's enabled."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(remote, "POLL_SECONDS", 0.05))
        self.started: list[remote.RemoteConfig] = []
        self.stopped = threading.Event()

        async def fake_run(config: remote.RemoteConfig, bottles: Any,
                           stop: threading.Event) -> None:
            del bottles
            self.started.append(config)
            await asyncio.to_thread(stop.wait)
            self.stopped.set()

        self.enterContext(mock.patch.object(remote, "run", fake_run))

    def _wait(self, condition: Any) -> bool:
        deadline = 2.0
        while deadline > 0 and not condition():
            threading.Event().wait(0.02)
            deadline -= 0.02
        return bool(condition())

    def test_enable_starts_and_disable_stops_the_link(self) -> None:
        supervisor = remote.Supervisor(_Bottles())
        supervisor.start()
        self.addCleanup(supervisor.close)
        self.assertFalse(self._wait(lambda: self.started))  # disabled: nothing runs
        remote.save_config(remote.RemoteConfig(True, "cid", "tk"))
        self.assertTrue(self._wait(lambda: self.started))
        remote.save_config(remote.RemoteConfig(False, "cid", "tk"))
        self.assertTrue(self.stopped.wait(2))
        self.assertTrue(self._wait(
            lambda: "disabled" in str(remote.read_status().get("error"))))

    def test_a_link_that_cannot_run_reports_why(self) -> None:
        async def failing(*_args: Any) -> None:
            raise remote.RemoteError("install `bot-bottle[remote]`")

        remote.save_config(remote.RemoteConfig(True, "cid", "tk"))
        with mock.patch.object(remote, "run", failing):
            supervisor = remote.Supervisor(_Bottles())
            supervisor.start()
            self.addCleanup(supervisor.close)
            self.assertTrue(self._wait(
                lambda: "bot-bottle[remote]" in str(remote.read_status().get("error"))))


async def _next_message(recv: Any, kind: str, timeout: float) -> dict[str, Any]:
    """The next control message of type `kind`, skipping others (a host sends
    `agents_running` first on every connect)."""
    buf = b""

    async def read() -> dict[str, Any]:
        nonlocal buf
        while True:
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                msg = json.loads(line)
                if msg.get("type") == kind:
                    return msg
            buf += await recv.read(4096)

    return await asyncio.wait_for(read(), timeout)

@unittest.skipIf(iroh is None, "needs the iroh bindings (the `remote` extra)")
class TestOverRealIroh(_RootCase):
    """A console-shaped endpoint driving the host's real `run()` loop."""

    def _session(self, *, pin_console: bool) -> dict[str, Any]:
        outcome: dict[str, Any] = {}
        lib: Any = iroh  # bound: the class is skipped without the bindings

        async def console_side() -> None:
            ep = await lib.Endpoint.bind(lib.EndpointOptions(
                preset=lib.preset_n0(), secret_key=os.urandom(32), alpns=[remote.ALPN]))
            pinned = str(ep.id()) if pin_console else str(
                lib.SecretKey.from_bytes(os.urandom(32)).public())
            remote.save_config(remote.RemoteConfig(
                True, pinned, str(lib.EndpointTicket.from_addr(ep.addr()))))
            stop = threading.Event()
            host = threading.Thread(target=lambda: asyncio.run(
                remote.run(remote.load_config(), _Bottles(), stop)), daemon=True)
            host.start()
            try:
                incoming = await asyncio.wait_for(ep.accept_next(), 15)
                conn = await (await incoming.accept()).connect()
                outcome["host_node"] = str(conn.remote_id())
                bi = await conn.open_bi()
                await bi.send().write_all(b'{"type":"list_local","request_id":"r1"}\n')
                outcome["reply"] = await _next_message(bi.recv(), "local_definitions", 3)
            except Exception as e:  # pylint: disable=broad-exception-caught  # refused case
                outcome["error"] = type(e).__name__
            finally:
                stop.set()
                host.join(timeout=15)
                await ep.close()

        with mock.patch.object(remote, "local_agents", return_value=["implementer"]):
            asyncio.run(console_side())
        outcome["status"] = remote.read_status()
        return outcome

    def test_stop_ends_a_connected_link_promptly(self) -> None:
        lib: Any = iroh
        elapsed: dict[str, float] = {}

        async def console_side() -> None:
            ep = await lib.Endpoint.bind(lib.EndpointOptions(
                preset=lib.preset_n0(), secret_key=os.urandom(32), alpns=[remote.ALPN]))
            remote.save_config(remote.RemoteConfig(
                True, str(ep.id()), str(lib.EndpointTicket.from_addr(ep.addr()))))
            stop = threading.Event()
            host = threading.Thread(target=lambda: asyncio.run(
                remote.run(remote.load_config(), _Bottles(), stop)), daemon=True)
            host.start()
            try:
                incoming = await asyncio.wait_for(ep.accept_next(), 15)
                # Hold the connection and its control stream open, as the
                # console does while a phone is paired.
                conn = await (await incoming.accept()).connect()
                bi = await conn.open_bi()
                await bi.send().write_all(b'{"type":"list_local","request_id":"r1"}\n')
                await _next_message(bi.recv(), "local_definitions", 5)
                started = time.monotonic()
                stop.set()
                await asyncio.to_thread(host.join, 15)
                elapsed["join"] = time.monotonic() - started
                elapsed["alive"] = float(host.is_alive())
            finally:
                await ep.close()

        with mock.patch.object(remote, "local_agents", return_value=[]):
            asyncio.run(console_side())
        self.assertFalse(elapsed["alive"])
        self.assertLess(elapsed["join"], 5)

    def test_the_host_dials_and_serves_the_pinned_console(self) -> None:
        outcome = self._session(pin_console=True)
        self.assertEqual(remote.node_id(), outcome["host_node"])
        self.assertEqual({"type": "local_definitions", "request_id": "r1",
                          "definitions": [{"name": "implementer"}]}, outcome["reply"])

    def test_the_host_refuses_a_console_it_did_not_pin(self) -> None:
        outcome = self._session(pin_console=False)
        self.assertNotIn("reply", outcome)
        self.assertIn("pinned", outcome["status"].get("error", ""))

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_a_launched_session_streams_and_takes_typing(self) -> None:
        """Launch over the link, then read the session's terminal stream and
        type into it: a real tmux session running `cat`, over real iroh."""
        import subprocess

        from bot_bottle.control.hostctl import sessions as sessions_mod

        sessions_mod.write_config()
        subprocess.run(sessions_mod.tmux("new-session", "-d", "-s", "bottle-1", "-x", "80",
                                         "-y", "24", "cat"), check=True)
        self.addCleanup(subprocess.run, sessions_mod.tmux("kill-server"), capture_output=True)
        manager = sessions_mod.SessionManager({})
        manager._write_record(sessions_mod.SessionRecord(  # pylint: disable=protected-access
            id="bottle-1", kind="bottle", writable_allowed=True))

        class _SessionBottles(_Bottles):
            sessions = manager

        outcome: dict[str, Any] = {}
        lib: Any = iroh

        async def console_side() -> None:
            ep = await lib.Endpoint.bind(lib.EndpointOptions(
                preset=lib.preset_n0(), secret_key=os.urandom(32), alpns=[remote.ALPN]))
            remote.save_config(remote.RemoteConfig(
                True, str(ep.id()), str(lib.EndpointTicket.from_addr(ep.addr()))))
            stop = threading.Event()
            host = threading.Thread(target=lambda: asyncio.run(
                remote.run(remote.load_config(), _SessionBottles(), stop)), daemon=True)
            host.start()
            try:
                incoming = await asyncio.wait_for(ep.accept_next(), 15)
                conn = await (await incoming.accept()).connect()
                bi = await conn.open_bi()
                send = bi.send()
                await send.write_all(json.dumps({
                    "type": "launch", "agent_id": "a1", "definition": {"name": "implementer"},
                    "prompt": "go"}).encode() + b"\n")
                outcome["started"] = await _next_message(bi.recv(), "agent_started", 10)
                uni = await asyncio.wait_for(conn.accept_uni(), 10)
                await asyncio.sleep(1.0)  # the relay's client attaches and turns writable
                await send.write_all(json.dumps({"type": "input", "agent_id": "a1",
                                                 "data": "typed-over-iroh\r"}).encode() + b"\n")
                received = b""
                deadline = asyncio.get_running_loop().time() + 10
                while b"typed-over-iroh" not in received and asyncio.get_running_loop().time() < deadline:
                    chunk = await asyncio.wait_for(uni.read(65536), 10)
                    if not chunk:
                        break
                    received += chunk
                outcome["terminal"] = received
            finally:
                stop.set()
                host.join(timeout=15)
                await ep.close()

        with mock.patch.object(remote, "local_agents", return_value=["implementer"]):
            asyncio.run(console_side())
        self.assertEqual("agent_started", outcome["started"]["type"])
        self.assertTrue(outcome["started"]["writable"])
        self.assertTrue(outcome["terminal"].startswith(b"a1\n"))
        self.assertIn(b"typed-over-iroh", outcome["terminal"])


class TestUnlessStopped(unittest.TestCase):
    def test_returns_the_result_or_cancels_once_stopped(self) -> None:
        async def go() -> tuple[object, bool]:
            stopped: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            quick = await remote._unless_stopped(asyncio.sleep(0, "done"), stopped)  # pylint: disable=protected-access
            cancelled = asyncio.Event()

            async def forever() -> None:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            asyncio.get_running_loop().call_later(0.05, stopped.set_result, None)
            with self.assertRaises(remote._Stopped):  # pylint: disable=protected-access
                await remote._unless_stopped(forever(), stopped)  # pylint: disable=protected-access
            return quick, cancelled.is_set()
        self.assertEqual(("done", True), asyncio.run(go()))


if __name__ == "__main__":
    unittest.main()
