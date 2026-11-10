"""The console refusing this host as `unregistered host` (its account was
deleted, or the host removed): the link reports it and waits long, and
`status`, `doctor` and home say to pair again. Nothing is changed on its own."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import link_refusal, pairing, remote, runner
from tests.unit import serve_host_controller, use_bottle_root

# What iroh's close_reason() gave for the console's close(1, b"unregistered host"),
# measured with two local endpoints.
REFUSED = "closed by peer: unregistered host (code 1)"
CONSOLE_ID = "c" * 64


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(use_bottle_root(self.root))


class TestIsUnregistered(unittest.TestCase):
    def test_the_consoles_refusal(self) -> None:
        self.assertTrue(link_refusal.is_unregistered(REFUSED))
        self.assertTrue(link_refusal.is_unregistered(f"  {REFUSED}\n"))

    def test_anything_else_isnt(self) -> None:
        for reason in (None, "", "closed by peer: host lookup failed (code 1)",
                       "closed by peer: unregistered host (code 2)",
                       "closed: unregistered host (code 1)", "timed out",
                       "closed by peer: host controller stopping (code 2)"):
            with self.subTest(reason=reason):
                self.assertFalse(link_refusal.is_unregistered(reason))

    def test_close_reason_is_best_effort(self) -> None:
        self.assertIsNone(link_refusal.close_reason(None))
        broken = mock.Mock(close_reason=mock.Mock(side_effect=RuntimeError("gone")))
        self.assertIsNone(link_refusal.close_reason(broken))
        self.assertIsNone(link_refusal.close_reason(mock.Mock(close_reason=lambda: 3)))
        refused = mock.Mock(close_reason=lambda: REFUSED)
        self.assertEqual(REFUSED, link_refusal.close_reason(refused))


class TestRefusedDelay(_Root):
    def test_a_refusal_is_recorded_and_waits_long(self) -> None:
        conn = mock.Mock(close_reason=lambda: REFUSED)
        with mock.patch.object(link_refusal.log, "warn") as warn:
            self.assertEqual(link_refusal.UNPAIRED_BACKOFF_SECONDS,
                             link_refusal.refused_delay(conn))
            link_refusal.refused_delay(conn)
        status = pairing.read_status()
        self.assertTrue(status["unpaired"])
        self.assertFalse(status["connected"])
        self.assertIn("bot-bottle pair", status["error"])
        self.assertTrue(link_refusal.unpaired())
        self.assertEqual(1, warn.call_count)  # once, not every ten minutes

    def test_other_closes_keep_the_backoff(self) -> None:
        for conn in (None, mock.Mock(close_reason=lambda: "timed out")):
            with self.subTest(conn=conn):
                self.assertEqual(0.0, link_refusal.refused_delay(conn))
        self.assertFalse(link_refusal.unpaired())


class _Stop(threading.Event):
    """Records the waits `run` asks for, and stops it after the first."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is None:
            return super().wait()
        self.waits.append(timeout)
        self.set()
        return True


def _iroh(reason: str, remote_id: str = CONSOLE_ID) -> Any:
    conn = mock.Mock()
    conn.remote_id.return_value = remote_id
    conn.close_reason.return_value = reason

    async def accept_bi() -> None:
        raise RuntimeError("")  # what iroh raises for a closed connection

    conn.accept_bi = accept_bi
    endpoint = mock.Mock()

    async def connect(_addr: Any, _alpn: bytes) -> Any:
        return conn

    async def close() -> None:
        return None

    endpoint.connect, endpoint.close = connect, close
    iroh = mock.Mock()

    async def bind(_options: Any) -> Any:
        return endpoint

    iroh.Endpoint.bind = bind
    return iroh


class TestRunLoop(_Root):
    def _run(self, reason: str) -> _Stop:
        stop = _Stop()
        config = pairing.RemoteConfig(enabled=True, console_node_id=CONSOLE_ID,
                                      console_ticket="ticket")
        with mock.patch.object(remote, "import_iroh", return_value=_iroh(reason)), \
                mock.patch.object(remote, "secret_key", return_value=b"k" * 32):
            asyncio.run(asyncio.wait_for(remote.run(config, mock.Mock(), stop), 10))
        return stop

    def test_a_refused_host_reports_unpaired_and_waits_ten_minutes(self) -> None:
        stop = self._run(REFUSED)
        self.assertEqual([link_refusal.UNPAIRED_BACKOFF_SECONDS], stop.waits)
        self.assertTrue(pairing.read_status()["unpaired"])

    def test_any_other_drop_keeps_the_short_backoff(self) -> None:
        stop = self._run("closed by peer: host lookup failed (code 1)")
        self.assertEqual([remote.BACKOFF_SECONDS[0]], stop.waits)
        self.assertFalse(pairing.read_status().get("unpaired"))

    def test_a_connection_that_is_accepted_clears_it(self) -> None:
        link_refusal.refused_delay(mock.Mock(close_reason=lambda: REFUSED))
        pairing.write_status(connected=True, console_node_id=CONSOLE_ID)
        self.assertFalse(link_refusal.unpaired())

    def test_nothing_is_changed_on_its_own(self) -> None:
        before = pairing.store().update(
            {"url": "https://console.example", "access_token": "t", "enabled": True,
             "console_node_id": CONSOLE_ID, "console_ticket": "ticket"}, new=True)
        key = pairing.secret_key()
        self._run(REFUSED)
        self.assertEqual(before, pairing.load_pairing())
        self.assertEqual("t", pairing.load_pairing().tokens()[0])
        self.assertEqual(key, pairing.secret_key())


class TestPairAgain(_Root):
    def test_new_credentials_replace_the_old_and_the_key_stays(self) -> None:
        """`bot-bottle pair` after a refusal: its credentials replace the
        deleted account's, under the same iroh key (so the same node id),
        and the next accepted connection clears the refusal."""
        pairing.store().update({"url": "https://console.example", "access_token": "old"},
                               new=True)
        key = pairing.secret_key()
        link_refusal.refused_delay(mock.Mock(close_reason=lambda: REFUSED))
        pairing.store().update({"url": "https://console.example", "access_token": "new"},
                               new=True)
        self.assertEqual("new", pairing.load_pairing().tokens()[0])
        self.assertEqual(key, pairing.secret_key())
        pairing.write_status(connected=True, console_node_id=CONSOLE_ID)
        self.assertFalse(link_refusal.unpaired())


class TestSurfaces(_Root):
    """The CLI's side, asking a running host controller."""

    def setUp(self) -> None:
        super().setUp()
        serve_host_controller(self)
        self.enterContext(mock.patch.object(runner, "running",
                                            return_value=runner.ensure_running()))
        self.enterContext(mock.patch.object(runner, "restart_needed", return_value=False))

    def _refuse(self) -> None:
        pairing.store().update(
            {"url": "https://console.example/", "access_token": "t", "enabled": True,
             "console_node_id": CONSOLE_ID, "console_ticket": "ticket"}, new=True)
        link_refusal.refused_delay(mock.Mock(close_reason=lambda: REFUSED))

    def test_status_says_refused(self) -> None:
        from bot_bottle.cli.commands import remote as remote_cmd  # pylint: disable=import-outside-toplevel
        self._refuse()
        line = remote_cmd.link_line()
        self.assertIn("refused, not paired anymore", line)
        self.assertIn("bot-bottle pair", line)

    def test_doctor_warns_and_next_is_pair(self) -> None:
        from bot_bottle.cli.commands import doctor  # pylint: disable=import-outside-toplevel
        self._refuse()
        found = doctor.BackendFindings(selected="docker", selected_ready=True, any_ready=True)
        with mock.patch.object(doctor, "_has_agents", return_value=True):
            self.assertEqual("`bot-bottle pair --console-url https://console.example` "
                             "(the console no longer knows this host)", doctor.next_step(found))
        with mock.patch.object(doctor, "_warn") as warn:
            doctor._check_console_link()  # pylint: disable=protected-access
        warn.assert_called_once()

    def test_doctor_without_a_console_url_still_says_pair(self) -> None:
        from bot_bottle.cli.commands import doctor  # pylint: disable=import-outside-toplevel
        link_refusal.refused_delay(mock.Mock(close_reason=lambda: REFUSED))
        self.assertEqual("`bot-bottle pair`", doctor._repair_command())  # pylint: disable=protected-access

    def test_doctor_says_nothing_when_not_refused(self) -> None:
        from bot_bottle.cli.commands import doctor  # pylint: disable=import-outside-toplevel
        self.assertEqual("", doctor._repair_command())  # pylint: disable=protected-access
        with mock.patch.object(doctor, "_warn") as warn:
            doctor._check_console_link()  # pylint: disable=protected-access
        warn.assert_not_called()

    def test_without_a_host_controller_nothing_is_said_or_started(self) -> None:
        from bot_bottle.cli.commands import doctor, home, remote as remote_cmd  # pylint: disable=import-outside-toplevel
        self._refuse()
        with mock.patch.object(runner, "running", return_value=None), \
                mock.patch.object(runner, "ensure_running") as start:
            self.assertEqual("", doctor._repair_command())  # pylint: disable=protected-access
            self.assertEqual("", home.console_state())
            self.assertIn("isn't running", remote_cmd.link_line())
        start.assert_not_called()

    def test_home_says_to_pair_again(self) -> None:
        from bot_bottle.cli.commands import home  # pylint: disable=import-outside-toplevel
        self._refuse()
        self.assertIn("pair` again", home.console_state())


if __name__ == "__main__":
    unittest.main()
