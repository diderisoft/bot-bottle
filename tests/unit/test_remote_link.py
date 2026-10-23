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
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import remote as remote_cmd
from bot_bottle.cli.commands.login import _save_credentials
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.control.hostctl import remote
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
        self.assertEqual([{"type": "agent_started", "agent_id": "a1", "bottle": "bottle-1"}],
                         sent)

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


class _RootCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="remote-root."))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.root))


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
                outcome["reply"] = json.loads(
                    await asyncio.wait_for(bi.recv().read(4096), 3))
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

    def test_the_host_dials_and_serves_the_pinned_console(self) -> None:
        outcome = self._session(pin_console=True)
        self.assertEqual(remote.node_id(), outcome["host_node"])
        self.assertEqual({"type": "local_definitions", "request_id": "r1",
                          "definitions": [{"name": "implementer"}]}, outcome["reply"])

    def test_the_host_refuses_a_console_it_did_not_pin(self) -> None:
        outcome = self._session(pin_console=False)
        self.assertNotIn("reply", outcome)
        self.assertIn("pinned", outcome["status"].get("error", ""))


if __name__ == "__main__":
    unittest.main()
