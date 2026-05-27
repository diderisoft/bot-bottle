"""Unit: sidecar bundle init supervisor (PRD 0024 chunk 1).

Tests both the helper functions in `claude_bottle.sidecar_init`
and the supervisor's end-to-end signal / exit-code behavior. The
end-to-end tests use real subprocesses (`/bin/sleep`,
`/bin/sh -c '...'`) — short-lived, no docker required — so they
run under `tests/unit/` rather than `tests/integration/`."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_bottle.sidecar_init import (
    _DaemonSpec,
    _Supervisor,
    _selected_daemons,
)


class TestSelectedDaemons(unittest.TestCase):
    """Env-var subset filtering. The compose renderer is the source
    of truth for which daemons are wired; the supervisor just
    honors what it's told."""

    _DAEMONS = (
        _DaemonSpec("egress", ("/bin/sh", "-c", ":")),
        _DaemonSpec("pipelock", ("/bin/sh", "-c", ":")),
        _DaemonSpec("git-gate", ("/bin/sh", "-c", ":")),
        _DaemonSpec("supervise", ("/bin/sh", "-c", ":")),
    )

    def test_unset_returns_all(self):
        got = _selected_daemons({}, all_daemons=self._DAEMONS)
        self.assertEqual([d.name for d in got],
                         ["egress", "pipelock", "git-gate", "supervise"])

    def test_empty_returns_all(self):
        got = _selected_daemons({"CLAUDE_BOTTLE_SIDECAR_DAEMONS": ""},
                                all_daemons=self._DAEMONS)
        self.assertEqual(4, len(got))

    def test_whitespace_only_returns_all(self):
        got = _selected_daemons({"CLAUDE_BOTTLE_SIDECAR_DAEMONS": "   "},
                                all_daemons=self._DAEMONS)
        self.assertEqual(4, len(got))

    def test_explicit_subset(self):
        got = _selected_daemons(
            {"CLAUDE_BOTTLE_SIDECAR_DAEMONS": "egress,pipelock"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress", "pipelock"])

    def test_preserves_canonical_order(self):
        # Order in the env var doesn't matter; the result follows
        # the canonical _DAEMONS order so egress starts before
        # pipelock (race-window reason).
        got = _selected_daemons(
            {"CLAUDE_BOTTLE_SIDECAR_DAEMONS": "supervise,pipelock,egress"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got],
                         ["egress", "pipelock", "supervise"])

    def test_unknown_names_ignored(self):
        got = _selected_daemons(
            {"CLAUDE_BOTTLE_SIDECAR_DAEMONS": "egress,bogus"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress"])

    def test_whitespace_in_names_stripped(self):
        got = _selected_daemons(
            {"CLAUDE_BOTTLE_SIDECAR_DAEMONS": " egress , pipelock "},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress", "pipelock"])


class TestSupervisor(unittest.TestCase):
    """End-to-end: drive `_Supervisor` directly with fake commands.
    We don't go through `main()` because main installs signal
    handlers process-wide, which collides with the test runner."""

    def _drive(self, sup: _Supervisor, max_wait_s: float = 6.0) -> int:
        deadline = time.monotonic() + max_wait_s
        while not sup.tick():
            if time.monotonic() > deadline:
                self.fail("supervisor watch loop did not converge in time")
            time.sleep(0.05)
        return sup.exit_code()

    def test_all_children_succeed_returns_zero(self):
        # `sh -c :` exits 0 immediately. With the new failure
        # policy a child dying doesn't trigger shutdown, so the
        # loop only converges once BOTH have exited on their own.
        # Both exit 0 → max(0, 0) = 0.
        specs = [
            _DaemonSpec("a", ("/bin/sh", "-c", ":")),
            _DaemonSpec("b", ("/bin/sh", "-c", ":")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        rc = self._drive(sup)
        self.assertEqual(0, rc)

    def test_child_crash_does_not_initiate_shutdown(self):
        # Failure policy (PRD 0024, interim): a child dying
        # unexpectedly is logged but the supervisor does NOT tear
        # down the survivors. Verified by giving the crasher
        # ~0.3s to die, then asserting the long-runner is still
        # up and the supervisor never set shutdown_at.
        specs = [
            _DaemonSpec("crasher", ("/bin/sh", "-c", "exit 1")),
            _DaemonSpec("longrun", ("/bin/sleep", "30")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        # Drive ticks for a while; crasher should die, longrun
        # should survive.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            done = sup.tick()
            self.assertFalse(done, "loop converged with a child still alive")
            if sup.procs[0][1].poll() is not None:
                break
            time.sleep(0.05)

        self.assertEqual(1, sup.procs[0][1].returncode,
                         "crasher should have exited 1")
        self.assertIsNone(sup.procs[1][1].poll(),
                          "longrun should still be running")
        self.assertIsNone(sup.shutdown_at,
                          "supervisor must not initiate shutdown on child death")

        # Clean up — explicit signal-driven shutdown.
        sup.request_shutdown(reason="test-teardown")
        self._drive(sup)

    def test_crash_then_signal_surfaces_nonzero_exit_code(self):
        # The crasher's exit code is what reaches the container
        # exit even though shutdown was triggered by SIGTERM.
        # exit_code() = max(child returncodes) → 1 wins over the
        # signal-killed longrun's negative returncode.
        specs = [
            _DaemonSpec("crasher", ("/bin/sh", "-c", "exit 1")),
            _DaemonSpec("longrun", ("/bin/sleep", "30")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.3)  # let crasher die
        sup.request_shutdown(reason="test")
        rc = self._drive(sup)
        self.assertEqual(1, rc)

    def test_all_children_die_unattended_loop_converges(self):
        # If nobody sends a signal but every child eventually
        # dies on its own, the supervisor still exits — nothing
        # left to supervise.
        specs = [
            _DaemonSpec("a", ("/bin/sh", "-c", "exit 0")),
            _DaemonSpec("b", ("/bin/sh", "-c", "exit 2")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        rc = self._drive(sup)
        self.assertEqual(2, rc)
        self.assertIsNone(sup.shutdown_at)

    def test_shutdown_after_start_terminates_children(self):
        # Two long-running children. Caller requests shutdown;
        # both should receive SIGTERM and exit. exit_code() is
        # max of (returncodes) — both signal-killed (negative),
        # so max() picks 0 in the typical case (or the
        # platform-specific signal returncode).
        specs = [
            _DaemonSpec("a", ("/bin/sleep", "60")),
            _DaemonSpec("b", ("/bin/sleep", "60")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.2)  # let them actually start
        sup.request_shutdown(reason="test")
        rc = self._drive(sup)
        self.assertIsNotNone(rc)
        # Both children got the signal — neither survived past
        # the grace deadline.
        for _, p in sup.procs:
            self.assertIsNotNone(p.returncode)

    def test_grace_period_escalates_to_sigkill(self):
        # A child that ignores SIGTERM. The supervisor's
        # _GRACE_SECONDS is 8s globally; we patch it to 0.3 so the
        # test stays fast.
        ignore_term = (
            "/bin/sh", "-c",
            "trap '' TERM; sleep 30",
        )
        specs = [_DaemonSpec("stubborn", ignore_term)]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.3)  # let `trap` register
        sup.request_shutdown(reason="test")

        with patch("claude_bottle.sidecar_init._GRACE_SECONDS", 0.3):
            rc = self._drive(sup, max_wait_s=4.0)

        # Process was SIGKILL'd → returncode -9 on POSIX.
        self.assertEqual(-9, sup.procs[0][1].returncode)
        self.assertIsNotNone(rc)

    def test_idempotent_shutdown_requests(self):
        specs = [_DaemonSpec("a", ("/bin/sleep", "60"))]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.1)
        first_at = None
        sup.request_shutdown(reason="first")
        first_at = sup.shutdown_at
        # Second call must NOT reset the deadline (otherwise the
        # grace timer is gameable by a noisy signal).
        sup.request_shutdown(reason="second")
        self.assertEqual(first_at, sup.shutdown_at)
        self._drive(sup)


class TestMainEndToEnd(unittest.TestCase):
    """Run sidecar_init.py as a real subprocess to cover the
    signal-handler installation path. Skipped on platforms
    without /bin/sleep + /bin/sh."""

    @classmethod
    def setUpClass(cls):
        for p in ("/bin/sh", "/bin/sleep"):
            if not Path(p).exists():
                raise unittest.SkipTest(f"missing {p}")

    def _run(self, daemons_csv: str, send_signal: int | None,
             wait_before_signal: float = 0.4,
             overall_timeout: float = 6.0) -> tuple[int, str]:
        """Spawn sidecar_init.main() in a child process with the
        DAEMONS list patched to harmless `sleep 30` commands.
        Returns (returncode, captured stdout)."""

        helper = (
            "import os, runpy, sys\n"
            "from claude_bottle import sidecar_init as si\n"
            "si._DAEMONS = (\n"
            "  si._DaemonSpec('alpha', ('/bin/sleep','30')),\n"
            "  si._DaemonSpec('beta',  ('/bin/sleep','30')),\n"
            ")\n"
            "sys.exit(si.main([]))\n"
        )
        env = {**os.environ, "CLAUDE_BOTTLE_SIDECAR_DAEMONS": daemons_csv}
        proc = subprocess.Popen(
            [sys.executable, "-c", helper],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            if send_signal is not None:
                time.sleep(wait_before_signal)
                proc.send_signal(send_signal)
            out_b, _ = proc.communicate(timeout=overall_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out_b, _ = proc.communicate()
            self.fail("sidecar_init main() did not exit before timeout")
        return proc.returncode, out_b.decode("utf-8", errors="replace")

    def test_sigterm_clean_shutdown(self):
        rc, out = self._run("alpha,beta", signal.SIGTERM)
        self.assertIn("starting alpha", out)
        self.assertIn("starting beta", out)
        self.assertIn("forwarding SIGTERM", out)
        # Sleep terminated by SIGTERM exits with returncode -15;
        # supervisor surfaces that via max(...) and main()
        # returns -15 → process exit becomes 256-15 = 241.
        # On macOS bash may convert to 143. Either way, nonzero
        # AND the child finished — we don't pin the exact code.
        self.assertNotEqual(0, rc)

    def test_empty_daemon_set_exits_zero_immediately(self):
        # Use a sentinel value that filters out both alpha+beta.
        rc, out = self._run("nothing", send_signal=None,
                            overall_timeout=2.0)
        self.assertEqual(0, rc)
        self.assertIn("no daemons selected", out)


if __name__ == "__main__":
    unittest.main()
