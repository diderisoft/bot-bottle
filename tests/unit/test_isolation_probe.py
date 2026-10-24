"""Unit: the launch-time isolation probe shared by the backends.

The guest is simulated in-process: a "leaky" guest really connects to the
canary listener the probe opens, so the probe's own detection is exercised."""

from __future__ import annotations

import contextlib
import io
import re
import socket
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


def _probe(
    run_guest: Callable[[str], str], *, check_dns: bool = False, host_gap: str = "",
) -> str:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        isolation_probe.verify_isolation(
            run_guest, what="the agent", remedy="fix it", check_dns=check_dns,
            host_gap=host_gap)
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


class TestHostGap(unittest.TestCase):
    """A backend with a tracked host gap warns on reaching the host and still
    fails closed on everything else."""

    def setUp(self) -> None:
        self.enterContext(mock.patch.object(
            isolation_probe, "host_canary_ip", return_value="127.0.0.1"))

    def test_reaching_the_host_warns_and_cites_the_gap(self) -> None:
        def guest(script: str) -> str:
            _leaky_guest(script)
            return _result(1)
        err = _probe(guest, host_gap="see #678")
        self.assertIn("isolation gap", err)
        self.assertIn("see #678", err)
        self.assertNotIn("isolation verified", err)

    def test_the_guest_keeps_checking_after_reaching_the_host(self) -> None:
        scripts: list[str] = []

        def guest(script: str) -> str:
            scripts.append(script)
            return _result(1)
        _probe(guest, host_gap="see #678")
        _probe(guest)
        self.assertIn("eh=0", scripts[0])
        self.assertIn("eh=1", scripts[1])

    def test_a_public_leak_still_fails_closed(self) -> None:
        def guest(script: str) -> str:
            _leaky_guest(script)
            return _result(4)
        with self.assertRaises(Die):
            _probe(guest, host_gap="see #678")

    def test_a_dns_leak_still_fails_closed(self) -> None:
        with self.assertRaises(Die):
            _probe(lambda _s: _result(3), check_dns=True, host_gap="see #678")

    def test_a_blocked_guest_is_verified(self) -> None:
        self.assertIn("isolation verified",
                      _probe(lambda _s: _result(1), host_gap="see #678"))


class TestNoCanary(unittest.TestCase):
    def test_skips_with_a_warning_when_the_host_has_no_route(self) -> None:
        guest = mock.Mock(return_value=_result(0))
        with mock.patch.object(isolation_probe, "host_canary_ip", return_value=""):
            self.assertIn("skipped", _probe(guest))
        guest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
