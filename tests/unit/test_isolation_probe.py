"""Unit: the launch-time isolation probe shared by the backends.

The guest is simulated in-process: a "leaky" guest really connects to the
canary listener the probe opens, so the probe's own detection is exercised."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from typing import Callable
from unittest import mock

from bot_bottle.backend import isolation_probe
from bot_bottle.lib.log import Die


def _canary(script: str) -> tuple[str, int]:
    m = re.match(r"h=(\S+); p=(\d+);", script)
    assert m, script
    return m.group(1), int(m.group(2))


def _result(n: int) -> str:
    return f"bb-isolation-probe={n}\n"


def _leaky_guest(script: str) -> str:
    with socket.create_connection(_canary(script), timeout=3):
        pass
    return _result(0)


def _probe(run_guest: Callable[[str], str], *, check_dns: bool = False) -> str:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        isolation_probe.verify_isolation(
            run_guest, what="the agent", remedy="fix it", check_dns=check_dns)
    return err.getvalue()


class TestVerifyIsolation(unittest.TestCase):
    def setUp(self) -> None:
        # The canary is the host's primary address; loopback stands in so the
        # test needs no network and the "leaky" guest can reach it.
        self.enterContext(mock.patch.object(
            isolation_probe, "host_canary_ip", return_value="127.0.0.1"))

    def test_a_blocked_guest_passes(self) -> None:
        self.assertIn("isolation verified", _probe(lambda _s: _result(1)))

    def test_a_guest_that_reaches_the_canary_fails_closed(self) -> None:
        with self.assertRaises(Die):
            _probe(_leaky_guest)

    def test_a_connection_counts_even_if_the_guest_reports_blocked(self) -> None:
        # The listener is the authority, not the guest's exit code.
        def lying_guest(script: str) -> str:
            _leaky_guest(script)
            return _result(1)
        with self.assertRaises(Die):
            _probe(lying_guest)

    def test_a_guest_that_reaches_another_host_address_fails_closed(self) -> None:
        # The first canary is blocked; the backend's extra host address isn't.
        def guest(script: str) -> str:
            more = re.search(r"more='?([\d.]+):(\d+)", script)
            assert more, script
            with socket.create_connection((more.group(1), int(more.group(2))), timeout=3):
                pass
            return _result(1)
        err = io.StringIO()
        with mock.patch.object(isolation_probe, "host_canary_ip", return_value="0.0.0.0"), \
                contextlib.redirect_stderr(err), self.assertRaises(Die) as cm:
            isolation_probe.verify_isolation(guest, what="the agent", remedy="fix it",
                                             host_ips=("127.0.0.1",))
        self.assertRegex(cm.exception.message, r"canary 127\.0\.0\.1:\d+\.")

    def test_every_host_address_is_named_when_blocked(self) -> None:
        err = io.StringIO()
        with mock.patch.object(isolation_probe, "host_canary_ip", return_value="0.0.0.0"), \
                contextlib.redirect_stderr(err):
            isolation_probe.verify_isolation(lambda _s: _result(1), what="the agent",
                                             remedy="fix it", host_ips=("127.0.0.1",))
        self.assertIn("0.0.0.0, 127.0.0.1", err.getvalue())

    def test_a_dns_leak_fails_closed(self) -> None:
        with self.assertRaises(Die) as cm:
            _probe(lambda _s: _result(3), check_dns=True)
        self.assertIn("DNS", cm.exception.message)

    def test_reaching_a_public_address_fails_closed(self) -> None:
        with self.assertRaises(Die) as cm:
            _probe(lambda _s: _result(4))
        self.assertIn("1.1.1.1", cm.exception.message)

    def test_no_tool_to_test_with_fails_closed(self) -> None:
        with self.assertRaises(Die):
            _probe(lambda _s: _result(2))

    def test_a_probe_that_did_not_run_fails_closed(self) -> None:
        # e.g. the exec itself failed (container gone, ssh error): it exits
        # non-zero with no result line. No evidence either way, so refuse.
        for out in ("", "Error: No such container: x\n", "bb-isolation-probe=9\n"):
            with self.subTest(out=out), self.assertRaises(Die):
                _probe(lambda _s, out=out: out)

    def test_the_dns_check_is_requested_only_when_asked(self) -> None:
        scripts: list[str] = []

        def guest(script: str) -> str:
            scripts.append(script)
            return _result(1)
        _probe(guest)
        _probe(guest, check_dns=True)
        self.assertIn("dns=0", scripts[0])
        self.assertIn("dns=1", scripts[1])


class TestPeers(unittest.TestCase):
    """Sibling agents: any answer from one is a leak."""

    def setUp(self) -> None:
        self.enterContext(mock.patch.object(
            isolation_probe, "host_canary_ip", return_value="127.0.0.1"))

    def _probe(self, run_guest: Callable[[str], str], peers: list[str]) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            isolation_probe.verify_isolation(
                run_guest, what="the agent", remedy="fix it", peers=peers)
        return err.getvalue()

    def test_a_sibling_that_answers_fails_closed_naming_it(self) -> None:
        out = "bb-isolation-peer=10.242.0.3\n" + _result(5)
        with self.assertRaises(Die) as cm:
            self._probe(lambda _s: out, ["10.242.0.3"])
        self.assertIn("sibling agent at 10.242.0.3", cm.exception.message)
        self.assertIn("fix it", cm.exception.message)

    def test_silent_siblings_pass_and_are_named(self) -> None:
        err = self._probe(lambda _s: _result(1), ["10.242.0.11", "10.242.0.3"])
        self.assertIn("or sibling agents 10.242.0.11, 10.242.0.3", err)

    def test_peers_are_knocked_on_at_the_unserved_port_deduped_and_capped(self) -> None:
        scripts: list[str] = []

        def guest(script: str) -> str:
            scripts.append(script)
            return _result(1)
        many = [f"10.0.0.{i}" for i in range(1, 10)] + ["10.0.0.1"]
        self._probe(guest, many)
        knocks = re.search(r"peers='([^']*)'", scripts[0])
        assert knocks, scripts[0]
        listed = knocks.group(1).split()
        self.assertEqual(isolation_probe.MAX_PEERS, len(listed))
        self.assertEqual(len(set(listed)), len(listed))
        self.assertTrue(all(k.endswith(f":{isolation_probe.PEER_PORT}") for k in listed))

    def test_no_peers_knocks_on_nothing(self) -> None:
        scripts: list[str] = []

        def guest(script: str) -> str:
            scripts.append(script)
            return _result(1)
        self._probe(guest, [])
        self.assertIn("peers=''", scripts[0])


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestGuestScriptPeerKnock(unittest.TestCase):
    """The guest script itself, run in a local shell: a refusal from a peer is
    an answer, so it reports a leak."""

    def _run(self, path: str | None = None) -> str:
        script = isolation_probe.guest_probe_script(
            "127.0.0.1", _closed_port(), check_dns=False, peers=["127.0.0.1"])
        env = {"PATH": path if path is not None else os.environ.get("PATH", "")}
        return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                              env=env, check=False, timeout=30).stdout

    @unittest.skipUnless(shutil.which("python3"), "needs python3")
    def test_python_counts_a_refusal_as_an_answer(self) -> None:
        out = self._run()
        self.assertIn("bb-isolation-peer=127.0.0.1", out)
        self.assertIn("bb-isolation-probe=5", out)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("timeout"),
                         "needs bash and timeout")
    def test_bash_counts_a_refusal_as_an_answer(self) -> None:
        # A PATH with bash and timeout but no python3.
        with tempfile.TemporaryDirectory() as d:
            for tool in ("bash", "timeout"):
                found = shutil.which(tool)
                assert found
                os.symlink(found, os.path.join(d, tool))
            out = self._run(path=d)
        self.assertIn("bb-isolation-probe=5", out)


class TestNoCanary(unittest.TestCase):
    def test_skips_with_a_warning_when_the_host_has_no_route(self) -> None:
        guest = mock.Mock(return_value=_result(0))
        with mock.patch.object(isolation_probe, "host_canary_ip", return_value=""):
            self.assertIn("skipped", _probe(guest))
        guest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
