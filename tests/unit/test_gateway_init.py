"""Unit: gateway data-plane init supervisor (PRD 0070; PRD 0024 bundle shape).

Tests both the helper functions in `bot_bottle.gateway.bootstrap`
and the supervisor's end-to-end signal / exit-code behavior. The
end-to-end tests use real subprocesses (`sleep`, `/bin/sh -c '...'`) —
short-lived, no docker required — so they run under `tests/unit/`
rather than `tests/integration/`."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from bot_bottle.gateway.bootstrap import (
    _DaemonSpec,
    _DaemonManager,
    _argv_for_daemon,
    _env_for_daemon,
    _pump,
    _selected_daemons,
)
from tests._bin import SLEEP


class TestEnvForDaemon(unittest.TestCase):
    """Scope egress-only credential env vars to the egress daemon.

    The agent never has access to EGRESS_TOKEN_* slots, so stripping
    them from non-egress daemons loses no DLP coverage."""

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

    def test_git_daemons_and_supervise_lose_egress_tokens(self):
        for name in ("git-gate", "git-http", "supervise"):
            env = _env_for_daemon(name, self._BASE)
            self.assertNotIn("EGRESS_TOKEN_0", env)
            self.assertNotIn("EGRESS_TOKEN_1", env)
            # Non-token bundle env stays — supervise / git-gate / git-http are
            # all load-bearing for other daemons.
            self.assertEqual("/usr/bin", env["PATH"])
            self.assertEqual("http://127.0.0.1:8888", env["EGRESS_UPSTREAM_PROXY"])
            self.assertEqual("9100", env["SUPERVISE_PORT"])

    def test_returns_independent_dict(self):
        # Caller mutation mustn't affect the original.
        env = _env_for_daemon("git-gate", self._BASE)
        env["X"] = "y"
        self.assertNotIn("X", self._BASE)


class TestSigningKeyScoping(unittest.TestCase):
    """The gateway never holds the control-plane signing key — it's the
    orchestrator's, which runs in a separate container/VM. Its daemons present
    the pre-minted `gateway` JWT instead; the key is stripped from every daemon's
    env as defense-in-depth (issue #469 review)."""

    _BASE = {
        "PATH": "/usr/bin",
        "BOT_BOTTLE_ORCHESTRATOR_TOKEN": "sk-x",
        "BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT": "gw-jwt",
    }

    def test_data_plane_daemons_get_jwt_not_key(self):
        for name in ("egress", "git-gate", "git-http", "supervise"):
            env = _env_for_daemon(name, self._BASE)
            self.assertNotIn("BOT_BOTTLE_ORCHESTRATOR_TOKEN", env, name)
            self.assertEqual("gw-jwt", env["BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT"], name)


class TestSelectedDaemons(unittest.TestCase):
    """Env-var subset filtering. The compose renderer is the source
    of truth for which daemons are wired; the supervisor just
    honors what it's told."""

    _DAEMONS = (
        _DaemonSpec("egress", ("/bin/sh", "-c", ":")),
        _DaemonSpec("git-gate", ("/bin/sh", "-c", ":")),
        _DaemonSpec("supervise", ("/bin/sh", "-c", ":")),
    )

    def test_unset_returns_all(self):
        got = _selected_daemons({}, all_daemons=self._DAEMONS)
        self.assertEqual([d.name for d in got],
                         ["egress", "git-gate", "supervise"])

    def test_empty_returns_all(self):
        got = _selected_daemons({"BOT_BOTTLE_GATEWAY_DAEMONS": ""},
                                all_daemons=self._DAEMONS)
        self.assertEqual(3, len(got))

    def test_whitespace_only_returns_all(self):
        got = _selected_daemons({"BOT_BOTTLE_GATEWAY_DAEMONS": "   "},
                                all_daemons=self._DAEMONS)
        self.assertEqual(3, len(got))

    def test_explicit_subset(self):
        got = _selected_daemons(
            {"BOT_BOTTLE_GATEWAY_DAEMONS": "egress,git-gate"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress", "git-gate"])

    def test_preserves_canonical_order(self):
        # Order in the env var doesn't matter; the result follows
        # the canonical _DAEMONS order so egress starts first.
        got = _selected_daemons(
            {"BOT_BOTTLE_GATEWAY_DAEMONS": "supervise,git-gate,egress"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got],
                         ["egress", "git-gate", "supervise"])

    def test_unknown_names_ignored(self):
        got = _selected_daemons(
            {"BOT_BOTTLE_GATEWAY_DAEMONS": "egress,bogus"},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress"])

    def test_whitespace_in_names_stripped(self):
        got = _selected_daemons(
            {"BOT_BOTTLE_GATEWAY_DAEMONS": " egress , git-gate "},
            all_daemons=self._DAEMONS,
        )
        self.assertEqual([d.name for d in got], ["egress", "git-gate"])


class TestDaemonArgv(unittest.TestCase):
    def test_git_daemons_wait_for_ready_marker_when_configured(self):
        argv = _argv_for_daemon(
            "git-gate",
            ("/bin/sh", "/git-gate-entrypoint.sh"),
            {"BOT_BOTTLE_GIT_GATE_READY_FILE": "/run/git-gate/ready"},
        )
        self.assertEqual("/bin/sh", argv[0])
        self.assertEqual("-c", argv[1])
        self.assertIn("BOT_BOTTLE_GIT_GATE_READY_FILE", argv[2])
        self.assertEqual("git-gate", argv[3])
        self.assertEqual(["/bin/sh", "/git-gate-entrypoint.sh"], argv[4:])

    def test_non_git_daemon_does_not_wait_for_ready_marker(self):
        argv = _argv_for_daemon(
            "egress",
            ("/bin/sh", "/app/egress-entrypoint.sh"),
            {"BOT_BOTTLE_GIT_GATE_READY_FILE": "/run/git-gate/ready"},
        )
        self.assertEqual(["/bin/sh", "/app/egress-entrypoint.sh"], argv)


class TestSupervisor(unittest.TestCase):
    """End-to-end: drive `_DaemonManager` directly with fake commands.
    We don't go through `main()` because main installs signal
    handlers process-wide, which collides with the test runner."""

    def setUp(self):
        warnings.simplefilter("error", ResourceWarning)
        self.addCleanup(warnings.resetwarnings)

    def _drive(self, sup: _DaemonManager, max_wait_s: float = 6.0) -> int:
        deadline = time.monotonic() + max_wait_s
        while not sup.tick():
            if time.monotonic() > deadline:
                self.fail("supervisor watch loop did not converge in time")
            time.sleep(0.05)
        return sup.exit_code()

    def test_all_children_succeed_returns_zero(self):
        # `sh -c :` exits 0 immediately. Start shutdown before driving
        # the loop so the intentionally short-lived fixtures are not
        # treated as unexpected deaths and restarted.
        specs = [
            _DaemonSpec("a", ("/bin/sh", "-c", ":")),
            _DaemonSpec("b", ("/bin/sh", "-c", ":")),
        ]
        sup = _DaemonManager(specs)
        sup.start_all()
        time.sleep(0.1)
        sup.request_shutdown(reason="test")
        rc = self._drive(sup)
        self.assertEqual(0, rc)

    def test_child_crash_triggers_restart_not_shutdown(self):
        # Failure policy: a child dying unexpectedly is restarted by the
        # supervisor rather than leaving egress dead. Verified by waiting for
        # the original pid to die, then confirming the supervisor spawned a
        # replacement with a different pid, and that shutdown was never requested.
        specs = [
            _DaemonSpec("crasher", ("/bin/sh", "-c", "exit 1")),
            _DaemonSpec("longrun", (SLEEP, "30")),
        ]
        sup = _DaemonManager(specs)
        sup.start_all()
        original_pid = sup.procs[0][1].pid

        # Drive ticks until the restart fires (crasher dies → restart queued →
        # next tick drains the queue and spawns a replacement).
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            sup.tick()
            if sup.procs[0][1].pid != original_pid:
                break
            time.sleep(0.05)

        self.assertNotEqual(original_pid, sup.procs[0][1].pid,
                            "crasher should have been restarted with a new pid")
        self.assertIsNone(sup.procs[1][1].poll(),
                          "longrun should still be running")
        self.assertIsNone(sup.shutdown_at,
                          "supervisor must not initiate shutdown on child death")

        # Clean up — explicit signal-driven shutdown.
        sup.request_shutdown(reason="test-teardown")
        self._drive(sup)

    def test_single_daemon_crash_is_restarted_before_tick_completes(self):
        specs = [_DaemonSpec("crasher", ("/bin/sh", "-c", "exit 1"))]
        sup = _DaemonManager(specs)
        sup.start_all()
        original_pid = sup.procs[0][1].pid
        time.sleep(0.1)

        done = sup.tick()

        self.assertFalse(done)
        self.assertNotEqual(original_pid, sup.procs[0][1].pid)
        self.assertEqual(set(), sup._restart_requested)
        self.assertIsNone(sup.shutdown_at)

        sup.request_shutdown(reason="test-teardown")
        self._drive(sup)

    def test_crash_then_signal_surfaces_nonzero_exit_code(self):
        # The crasher's exit code is what reaches the container
        # exit even though shutdown was triggered by SIGTERM.
        # exit_code() = max(child returncodes) → 1 wins over the
        # signal-killed longrun's negative returncode.
        specs = [
            _DaemonSpec("crasher", ("/bin/sh", "-c", "exit 1")),
            _DaemonSpec("longrun", (SLEEP, "30")),
        ]
        sup = _DaemonManager(specs)
        sup.start_all()
        time.sleep(0.3)  # let crasher die
        sup.request_shutdown(reason="test")
        rc = self._drive(sup)
        self.assertEqual(1, rc)

    def test_all_children_die_unattended_are_restarted(self):
        specs = [
            _DaemonSpec("a", ("/bin/sh", "-c", "exit 0")),
            _DaemonSpec("b", ("/bin/sh", "-c", "exit 2")),
        ]
        sup = _DaemonManager(specs)
        sup.start_all()
        original_pids = [p.pid for _, p in sup.procs]
        time.sleep(0.1)

        done = sup.tick()

        self.assertFalse(done)
        self.assertNotEqual(original_pids, [p.pid for _, p in sup.procs])
        self.assertIsNone(sup.shutdown_at)

        sup.request_shutdown(reason="test-teardown")
        self._drive(sup)

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
            _DaemonSpec("other",  (SLEEP, "30")),
        ]
        sup = _DaemonManager(specs)
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
        specs = [_DaemonSpec("a", (SLEEP, "30"))]
        sup = _DaemonManager(specs)
        sup.start_all()
        delivered = sup.forward_signal(signal.SIGHUP, "ghost")
        self.assertFalse(delivered)
        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_restart_daemon_replaces_in_place(self):
        # Restart one daemon; the other (supervise, the MCP server
        # in production) must remain untouched.
        specs = [
            _DaemonSpec("git-gate", (SLEEP, "30")),
            _DaemonSpec("supervise", (SLEEP, "30")),
        ]
        sup = _DaemonManager(specs)
        sup.start_all()
        time.sleep(0.1)
        old_git_gate_pid = sup.procs[0][1].pid
        supervise_pid = sup.procs[1][1].pid

        ok = sup.restart_daemon("git-gate", grace=2.0)
        self.assertTrue(ok)

        # git-gate got a fresh PID — different process.
        new_git_gate_pid = sup.procs[0][1].pid
        self.assertNotEqual(old_git_gate_pid, new_git_gate_pid)
        # Supervise's PID is unchanged — it was NOT restarted.
        self.assertEqual(supervise_pid, sup.procs[1][1].pid)
        self.assertIsNone(sup.procs[1][1].poll(),
                          "supervise should still be running")

        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_request_restart_is_drained_by_tick(self):
        specs = [
            _DaemonSpec("git-gate", (SLEEP, "30")),
            _DaemonSpec("supervise", (SLEEP, "30")),
        ]
        sup = _DaemonManager(specs)
        sup.start_all()
        time.sleep(0.1)
        old_git_gate_pid = sup.procs[0][1].pid
        supervise_pid = sup.procs[1][1].pid

        ok = sup.request_restart("git-gate")
        self.assertTrue(ok)
        # The non-blocking request path only records intent.
        self.assertEqual(old_git_gate_pid, sup.procs[0][1].pid)

        done = sup.tick()
        self.assertFalse(done)

        self.assertNotEqual(old_git_gate_pid, sup.procs[0][1].pid)
        self.assertEqual(supervise_pid, sup.procs[1][1].pid)

        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_repeated_restart_requests_coalesce(self):
        specs = [_DaemonSpec("git-gate", (SLEEP, "30"))]
        sup = _DaemonManager(specs)
        sup.start_all()
        time.sleep(0.1)

        self.assertTrue(sup.request_restart("git-gate"))
        self.assertTrue(sup.request_restart("git-gate"))
        self.assertEqual({"git-gate"}, sup._restart_requested)

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
        specs = [_DaemonSpec("a", (SLEEP, "30"))]
        sup = _DaemonManager(specs)
        sup.start_all()
        ok = sup.request_restart("ghost")
        self.assertFalse(ok)
        self.assertEqual(set(), sup._restart_requested)
        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_restart_unknown_daemon_no_op(self):
        specs = [_DaemonSpec("a", (SLEEP, "30"))]
        sup = _DaemonManager(specs)
        sup.start_all()
        ok = sup.restart_daemon("ghost")
        self.assertFalse(ok)
        sup.request_shutdown(reason="cleanup")
        self._drive(sup)

    def test_restart_during_shutdown_is_no_op(self):
        specs = [_DaemonSpec("git-gate", (SLEEP, "30"))]
        sup = _DaemonManager(specs)
        sup.start_all()
        sup.request_shutdown(reason="test")
        ok = sup.restart_daemon("git-gate")
        self.assertFalse(ok,
                         "must not respawn a daemon during teardown")
        self._drive(sup)

    def test_pending_restart_dropped_during_shutdown(self):
        specs = [_DaemonSpec("git-gate", (SLEEP, "30"))]
        sup = _DaemonManager(specs)
        sup.start_all()
        time.sleep(0.1)
        old_pid = sup.procs[0][1].pid

        self.assertTrue(sup.request_restart("git-gate"))
        sup.request_shutdown(reason="test")
        self.assertEqual(set(), sup._restart_requested)
        self._drive(sup)

        self.assertEqual(old_pid, sup.procs[0][1].pid)

    def test_shutdown_after_start_terminates_children(self):
        # Two long-running children. Caller requests shutdown;
        # both should receive SIGTERM and exit. Signal-only
        # shutdown clamps to a zero supervisor exit code.
        specs = [
            _DaemonSpec("a", (SLEEP, "60")),
            _DaemonSpec("b", (SLEEP, "60")),
        ]
        sup = _DaemonManager(specs)
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
        sup = _DaemonManager(specs)
        sup.start_all()
        time.sleep(0.3)  # let `trap` register
        sup.request_shutdown(reason="test")

        with patch("bot_bottle.gateway.bootstrap._GRACE_SECONDS", 0.3):
            rc = self._drive(sup, max_wait_s=4.0)

        # Process was SIGKILL'd → returncode -9 on POSIX.
        self.assertEqual(-9, sup.procs[0][1].returncode)
        self.assertEqual(0, rc)

    def test_idempotent_shutdown_requests(self):
        specs = [_DaemonSpec("a", (SLEEP, "60"))]
        sup = _DaemonManager(specs)
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
    """Run gateway_init.py as a real subprocess to cover the
    signal-handler installation path. Skipped on platforms
    without /bin/sleep + /bin/sh."""

    @classmethod
    def setUpClass(cls):
        for p in ("/bin/sh", SLEEP):
            if not Path(p).exists():
                raise unittest.SkipTest(f"missing {p}")

    def _run(self, daemons_csv: str, send_signal: int | None,
             wait_before_signal: float = 0.4,
             overall_timeout: float = 6.0) -> tuple[int, str]:
        """Spawn gateway_init.main() in a child process with the
        DAEMONS list patched to harmless `sleep 30` commands.
        Returns (returncode, captured stdout)."""

        helper = (
            "import os, runpy, sys\n"
            "from bot_bottle.gateway import bootstrap as si\n"
            "si._DAEMONS = (\n"
            f"  si._DaemonSpec('alpha', ({SLEEP!r},'30')),\n"
            f"  si._DaemonSpec('beta',  ({SLEEP!r},'30')),\n"
            ")\n"
            "sys.exit(si.main([]))\n"
        )
        env = {**os.environ, "BOT_BOTTLE_GATEWAY_DAEMONS": daemons_csv}
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
            self.fail("gateway_init main() did not exit before timeout")
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


class _FailingStream:
    def __init__(self, *, closed: bool) -> None:
        self.closed = closed

    def readline(self) -> bytes:
        raise ValueError("I/O operation on closed file")


class TestOutputPump(unittest.TestCase):
    def test_closed_stream_race_is_normal_completion(self) -> None:
        with patch("bot_bottle.gateway.bootstrap._log") as log:
            _pump("egress", _FailingStream(closed=True))  # type: ignore[arg-type]
        log.assert_not_called()

    def test_unexpected_io_failure_is_logged(self) -> None:
        with patch("bot_bottle.gateway.bootstrap._log") as log:
            _pump("egress", _FailingStream(closed=False))  # type: ignore[arg-type]
        log.assert_called_once()
        self.assertIn("output pump stopped", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
