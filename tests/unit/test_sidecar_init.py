"""Unit: sidecar bundle init supervisor (PRD 0024 chunk 1).

Tests both the helper functions in `bot_bottle.sidecar_init`
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

from bot_bottle.sidecar_init import (
    _DaemonSpec,
    _Supervisor,
    _env_for_daemon,
    _selected_daemons,
)


class TestEnvForDaemon(unittest.TestCase):
    """Scope egress-only credential env vars to the egress daemon.

    Regression for issue #84: pipelock's `scan_env: true` matched
    `EGRESS_TOKEN_*` against egress's just-injected Authorization
    header and 403-blocked the legitimate request. The agent
    never has access to these slots, so stripping them from
    non-egress daemons loses no DLP coverage."""

    _BASE = {
        "PATH": "/usr/bin",
        "EGRESS_TOKEN_0": "tok-anthropic",
        "EGRESS_TOKEN_1": "tok-gitea",
        "EGRESS_UPSTREAM_PROXY": "http://127.0.0.1:8888",
        "SUPERVISE_PORT": "9100",
    }

    def test_egress_sees_full_env(self):
        env = _env_for_daemon("egress", self._BASE)
        self.assertEqual(self._BASE, env)

    def test_pipelock_loses_egress_tokens(self):
        env = _env_for_daemon("pipelock", self._BASE)
        self.assertNotIn("EGRESS_TOKEN_0", env)
        self.assertNotIn("EGRESS_TOKEN_1", env)
        # Non-token bundle env stays — supervise / git-gate / git-http / the
        # upstream proxy URL are all load-bearing for other
        # daemons.
        self.assertEqual("/usr/bin", env["PATH"])
        self.assertEqual("http://127.0.0.1:8888", env["EGRESS_UPSTREAM_PROXY"])
        self.assertEqual("9100", env["SUPERVISE_PORT"])

    def test_git_daemons_and_supervise_also_lose_egress_tokens(self):
        for name in ("git-gate", "git-http", "supervise"):
            env = _env_for_daemon(name, self._BASE)
            self.assertNotIn("EGRESS_TOKEN_0", env)
            self.assertNotIn("EGRESS_TOKEN_1", env)

    def test_returns_independent_dict(self):
        # Caller mutation mustn't affect the original.
        env = _env_for_daemon("pipelock", self._BASE)
        env["X"] = "y"
        self.assertNotIn("X", self._BASE)


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
        got = _selected_daemons({"BOT_BOTTLE_SIDECAR_DAEMONS": ""},
                                all_daemons=self._DAEMONS)
        self.assertEqual(4, len(got))

    def test_whitespace_only_returns_all(self):
        got = _selected_daemons({"BOT_BOTTLE_SIDECAR_DAEMONS": "   "},
                                all_daemons=self._DAEMONS)
        self.assertEqual(4, len(got))

    def test_explicit_subset(self):
        got = _selected_daemons(
            {"BOT_BOTTLE_SIDECAR_DAEMONS": "egress,pipelock"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress", "pipelock"])

    def test_preserves_canonical_order(self):
        # Order in the env var doesn't matter; the result follows
        # the canonical _DAEMONS order so egress starts before
        # pipelock (race-window reason).
        got = _selected_daemons(
            {"BOT_BOTTLE_SIDECAR_DAEMONS": "supervise,pipelock,egress"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got],
                         ["egress", "pipelock", "supervise"])

    def test_unknown_names_ignored(self):
        got = _selected_daemons(
            {"BOT_BOTTLE_SIDECAR_DAEMONS": "egress,bogus"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress"])

    def test_whitespace_in_names_stripped(self):
        got = _selected_daemons(
            {"BOT_BOTTLE_SIDECAR_DAEMONS": " egress , pipelock "},
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

    def test_forward_signal_to_named_child(self):
        # SIGHUP needs to reach mitmdump inside the bundle so
        # routes.yaml reloads (egress_apply.py issues `docker kill
        # --signal HUP <bundle>`). The supervisor forwards by daemon
        # name.
        #
        # The child is Python (not a shell-with-trap) on purpose:
        # bash on macOS defers trap execution while a foreground
        # builtin like `sleep` is running and stdout is a pipe,
        # which makes shell-trap test fixtures flaky. The
        # production code path (mitmdump as the bundle child)
        # doesn't have a shell in between — egress_entrypoint.sh
        # `exec`s mitmdump, so SIGHUP lands directly on mitmdump.
        sighup_marker = (
            sys.executable, "-c",
            "import signal, sys, time\n"
            "def _h(*_): sys.exit(42)\n"
            "signal.signal(signal.SIGHUP, _h)\n"
            "while True: time.sleep(0.1)\n",
        )
        specs = [
            _DaemonSpec("egress", sighup_marker),
            _DaemonSpec("other",  ("/bin/sleep", "30")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.3)  # let Python install the handler

        delivered = sup.forward_signal(signal.SIGHUP, "egress")
        self.assertTrue(delivered)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if sup.procs[0][1].poll() is not None:
                break
            time.sleep(0.05)
        self.assertEqual(42, sup.procs[0][1].returncode,
                         "egress did not see SIGHUP")
        # The other daemon is untouched.
        self.assertIsNone(sup.procs[1][1].poll())

        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_forward_signal_unknown_daemon_no_op(self):
        specs = [_DaemonSpec("a", ("/bin/sleep", "30"))]
        sup = _Supervisor(specs)
        sup.start_all()
        delivered = sup.forward_signal(signal.SIGHUP, "ghost")
        self.assertFalse(delivered)
        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_restart_daemon_replaces_in_place(self):
        # pipelock_apply.py sends SIGUSR1 to the bundle, supervisor
        # restarts the pipelock daemon, supervise (the other
        # daemon's MCP server in production) stays up.
        specs = [
            _DaemonSpec("pipelock", ("/bin/sleep", "30")),
            _DaemonSpec("supervise", ("/bin/sleep", "30")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.1)
        old_pipelock_pid = sup.procs[0][1].pid
        supervise_pid = sup.procs[1][1].pid

        ok = sup.restart_daemon("pipelock", grace=2.0)
        self.assertTrue(ok)

        # Pipelock got a fresh PID — different process.
        new_pipelock_pid = sup.procs[0][1].pid
        self.assertNotEqual(old_pipelock_pid, new_pipelock_pid)
        # Supervise's PID is unchanged — it was NOT restarted.
        self.assertEqual(supervise_pid, sup.procs[1][1].pid)
        self.assertIsNone(sup.procs[1][1].poll(),
                          "supervise should still be running")

        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_request_restart_is_drained_by_tick(self):
        specs = [
            _DaemonSpec("pipelock", ("/bin/sleep", "30")),
            _DaemonSpec("supervise", ("/bin/sleep", "30")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.1)
        old_pipelock_pid = sup.procs[0][1].pid
        supervise_pid = sup.procs[1][1].pid

        ok = sup.request_restart("pipelock")
        self.assertTrue(ok)
        # The non-blocking request path only records intent.
        self.assertEqual(old_pipelock_pid, sup.procs[0][1].pid)

        done = sup.tick()
        self.assertFalse(done)

        self.assertNotEqual(old_pipelock_pid, sup.procs[0][1].pid)
        self.assertEqual(supervise_pid, sup.procs[1][1].pid)

        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_repeated_restart_requests_coalesce(self):
        specs = [_DaemonSpec("pipelock", ("/bin/sleep", "30"))]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.1)

        self.assertTrue(sup.request_restart("pipelock"))
        self.assertTrue(sup.request_restart("pipelock"))
        self.assertEqual({"pipelock"}, sup._restart_requested)

        old_pid = sup.procs[0][1].pid
        sup.tick()
        first_restarted_pid = sup.procs[0][1].pid
        self.assertNotEqual(old_pid, first_restarted_pid)

        # A second tick should not restart again; the coalesced
        # request was consumed by the first tick.
        sup.tick()
        self.assertEqual(first_restarted_pid, sup.procs[0][1].pid)

        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_request_restart_unknown_daemon_no_op(self):
        specs = [_DaemonSpec("a", ("/bin/sleep", "30"))]
        sup = _Supervisor(specs)
        sup.start_all()
        ok = sup.request_restart("ghost")
        self.assertFalse(ok)
        self.assertEqual(set(), sup._restart_requested)
        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_restart_unknown_daemon_no_op(self):
        specs = [_DaemonSpec("a", ("/bin/sleep", "30"))]
        sup = _Supervisor(specs)
        sup.start_all()
        ok = sup.restart_daemon("ghost")
        self.assertFalse(ok)
        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_restart_during_shutdown_is_no_op(self):
        specs = [_DaemonSpec("pipelock", ("/bin/sleep", "30"))]
        sup = _Supervisor(specs)
        sup.start_all()
        sup.request_shutdown(reason="test")
        ok = sup.restart_daemon("pipelock")
        self.assertFalse(ok,
                         "must not respawn a daemon during teardown")
        self._drive(sup)

    def test_pending_restart_dropped_during_shutdown(self):
        specs = [_DaemonSpec("pipelock", ("/bin/sleep", "30"))]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.1)
        old_pid = sup.procs[0][1].pid

        self.assertTrue(sup.request_restart("pipelock"))
        sup.request_shutdown(reason="test")
        self.assertEqual(set(), sup._restart_requested)
        self._drive(sup)

        self.assertEqual(old_pid, sup.procs[0][1].pid)

    def test_shutdown_after_start_terminates_children(self):
        # Two long-running children. Caller requests shutdown;
        # both should receive SIGTERM and exit. Signal-only
        # shutdown clamps to a zero supervisor exit code.
        specs = [
            _DaemonSpec("a", ("/bin/sleep", "60")),
            _DaemonSpec("b", ("/bin/sleep", "60")),
        ]
        sup = _Supervisor(specs)
        sup.start_all()
        time.sleep(0.2)  # let them actually start
        sup.request_shutdown(reason="test")
        rc = self._drive(sup)
        self.assertEqual(0, rc)
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

        with patch("bot_bottle.sidecar_init._GRACE_SECONDS", 0.3):
            rc = self._drive(sup, max_wait_s=4.0)

        # Process was SIGKILL'd → returncode -9 on POSIX.
        self.assertEqual(-9, sup.procs[0][1].returncode)
        self.assertEqual(0, rc)

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
            "from bot_bottle import sidecar_init as si\n"
            "si._DAEMONS = (\n"
            "  si._DaemonSpec('alpha', ('/bin/sleep','30')),\n"
            "  si._DaemonSpec('beta',  ('/bin/sleep','30')),\n"
            ")\n"
            "sys.exit(si.main([]))\n"
        )
        env = {**os.environ, "BOT_BOTTLE_SIDECAR_DAEMONS": daemons_csv}
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
        self.assertEqual(0, rc)

    def test_empty_daemon_set_exits_zero_immediately(self):
        # Use a sentinel value that filters out both alpha+beta.
        rc, out = self._run("nothing", send_signal=None,
                            overall_timeout=2.0)
        self.assertEqual(0, rc)
        self.assertIn("no daemons selected", out)


if __name__ == "__main__":
    unittest.main()
