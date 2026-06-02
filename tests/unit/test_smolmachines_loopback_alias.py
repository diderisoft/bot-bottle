"""Unit: per-bottle loopback alias pool (follow-up to the
Docker-Desktop fix in PR #74).

`ensure_pool` lazily sudo-adds missing aliases on macOS; no-ops
on Linux. `allocate` picks the lowest-numbered unused alias by
inspecting running bundle containers' port bindings."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.smolmachines import loopback_alias


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr="",
    )


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr,
    )


# `ifconfig lo0` on macOS with the default lo0 config: just
# 127.0.0.1. We craft fixtures around this shape.
_LO0_DEFAULT = (
    "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
    "\tinet 127.0.0.1 netmask 0xff000000\n"
    "\tinet6 ::1 prefixlen 128\n"
)

_LO0_PARTIAL = (
    _LO0_DEFAULT
    + "\tinet 127.0.0.16 netmask 0xffffffff\n"
    + "\tinet 127.0.0.17 netmask 0xffffffff\n"
)


def _lo0_full() -> str:
    """All 16 pool addresses already aliased."""
    aliases = "".join(
        f"\tinet 127.0.0.{i} netmask 0xffffffff\n"
        for i in range(16, 32)
    )
    return _LO0_DEFAULT + aliases


class TestEnsurePool(unittest.TestCase):
    def test_noop_on_linux(self):
        # `_is_macos` returns False on Linux; ensure_pool should
        # never shell out to sudo.
        with patch.object(loopback_alias, "_is_macos", return_value=False), \
             patch.object(loopback_alias.subprocess, "run") as run:
            loopback_alias.ensure_pool()
        run.assert_not_called()

    def test_all_present_skips_sudo(self):
        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(
                 loopback_alias.subprocess, "run",
                 return_value=_ok(stdout=_lo0_full()),
             ) as run:
            loopback_alias.ensure_pool()
        # Just the ifconfig probe per pool address; no sudo at all.
        for call in run.call_args_list:
            self.assertNotIn("sudo", call.args[0])

    def test_missing_aliases_dispatch_sudo(self):
        # lo0 only has 16+17 already; sudo runs for 18..31 (14 missing).
        runs: list[list[str]] = []

        def fake_run(argv, *a, **kw):
            runs.append(argv)
            if argv[:2] == ["/sbin/ifconfig", "lo0"]:
                return _ok(stdout=_LO0_PARTIAL)
            return _ok()

        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(loopback_alias.subprocess, "run", side_effect=fake_run):
            loopback_alias.ensure_pool()

        sudo_calls = [r for r in runs if r and r[0] == "sudo"]
        self.assertEqual(14, len(sudo_calls))
        sudo_ips = {call[call.index("alias") + 1].split("/")[0] for call in sudo_calls}
        self.assertEqual(
            {f"127.0.0.{i}" for i in range(18, 32)},
            sudo_ips,
        )

    def test_sudo_failure_dies(self):
        def fake_run(argv, *a, **kw):
            if argv[:2] == ["/sbin/ifconfig", "lo0"]:
                return _ok(stdout=_LO0_DEFAULT)
            if argv[:1] == ["sudo"]:
                return _fail()
            return _ok()

        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(loopback_alias.subprocess, "run", side_effect=fake_run), \
             patch.object(loopback_alias, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                loopback_alias.ensure_pool()


class TestAllocate(unittest.TestCase):
    def test_returns_loopback_on_linux(self):
        with patch.object(loopback_alias, "_is_macos", return_value=False):
            self.assertEqual("127.0.0.1", loopback_alias.allocate("demo"))

    def test_picks_lowest_unused_on_macos(self):
        # No bundles running -> first pool entry.
        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(loopback_alias, "_aliases_in_use", return_value=set()):
            self.assertEqual("127.0.0.16", loopback_alias.allocate("demo-1"))

    def test_skips_in_use_aliases(self):
        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(
                 loopback_alias, "_aliases_in_use",
                 return_value={"127.0.0.16", "127.0.0.17", "127.0.0.19"},
             ):
            # First unused = 127.0.0.18.
            self.assertEqual("127.0.0.18", loopback_alias.allocate("demo-3"))

    def test_dies_when_pool_exhausted(self):
        all_in_use = {f"127.0.0.{i}" for i in range(16, 32)}
        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(
                 loopback_alias, "_aliases_in_use",
                 return_value=all_in_use,
             ), patch.object(
                 loopback_alias, "die", side_effect=SystemExit("die"),
             ):
            with self.assertRaises(SystemExit):
                loopback_alias.allocate("demo-overflow")


class TestAllocateLock(unittest.TestCase):
    """allocate() on macOS acquires a file lock so concurrent calls
    serialise rather than racing on docker state."""

    def test_acquires_exclusive_lock_on_macos(self):
        import fcntl as fcntl_mod
        flock_calls: list[int] = []

        def record_flock(fd, op):
            flock_calls.append(op)

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "smolmachines.lock"
            with patch.object(loopback_alias, "_is_macos", return_value=True), \
                 patch.object(loopback_alias, "_ALLOC_LOCK_PATH", lock_path), \
                 patch.object(loopback_alias, "_aliases_in_use", return_value=set()), \
                 patch.object(loopback_alias.fcntl, "flock",
                              side_effect=record_flock):
                loopback_alias.allocate("demo")

        self.assertIn(fcntl_mod.LOCK_EX, flock_calls)

    def test_no_lock_on_linux(self):
        # Linux early-returns before touching the lock file.
        with patch.object(loopback_alias, "_is_macos", return_value=False), \
             patch.object(loopback_alias.fcntl, "flock") as flock:
            loopback_alias.allocate("demo")
        flock.assert_not_called()

    def test_sequential_allocations_with_shared_lock_are_serialised(self):
        # Two sequential calls share the same lock file. The second
        # call sees {127.0.0.16} in use (as if the first caller's
        # docker run completed between the two lock acquisitions) and
        # returns the next alias.
        in_use_seq = [set(), {"127.0.0.16"}]

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "smolmachines.lock"
            results: list[str] = []
            for _ in range(2):
                with patch.object(loopback_alias, "_is_macos", return_value=True), \
                     patch.object(loopback_alias, "_ALLOC_LOCK_PATH", lock_path), \
                     patch.object(loopback_alias, "_aliases_in_use",
                                  return_value=in_use_seq.pop(0)):
                    results.append(loopback_alias.allocate("demo"))

        self.assertEqual(["127.0.0.16", "127.0.0.17"], results)


class TestAliasInUseDetection(unittest.TestCase):
    """`_aliases_in_use` inspects every running bundle and pulls
    each container's port-binding `HostIp` out. The detection has
    to survive: no running bundles, multiple bundles, docker
    inspect failures."""

    def test_no_bundles_returns_empty(self):
        with patch.object(
            loopback_alias.subprocess, "run",
            return_value=_ok(stdout=""),
        ):
            self.assertEqual(set(), loopback_alias._aliases_in_use())

    def test_walks_bundles_and_pulls_host_ips(self):
        # First call: docker ps -> two bundle names.
        # Then docker inspect each, returning a port-bindings JSON
        # blob with a HostIp on the per-bottle alias.
        ps_out = "bot-bottle-sidecars-a\nbot-bottle-sidecars-b\n"
        inspect_a = (
            '{"8888/tcp":[{"HostIp":"127.0.0.16","HostPort":"54000"}]}'
        )
        inspect_b = (
            '{"9099/tcp":[{"HostIp":"127.0.0.17","HostPort":"54001"}]}'
        )

        seq = [
            _ok(stdout=ps_out),
            _ok(stdout=inspect_a),
            _ok(stdout=inspect_b),
        ]
        with patch.object(
            loopback_alias.subprocess, "run", side_effect=seq,
        ):
            self.assertEqual(
                {"127.0.0.16", "127.0.0.17"},
                loopback_alias._aliases_in_use(),
            )

    def test_inspect_failures_are_skipped(self):
        ps_out = "bot-bottle-sidecars-c\n"
        with patch.object(
            loopback_alias.subprocess, "run",
            side_effect=[_ok(stdout=ps_out), _fail("inspect failed")],
        ):
            self.assertEqual(set(), loopback_alias._aliases_in_use())


class TestForceAllowlist(unittest.TestCase):
    """Smolvm 0.8.0 silently drops `--allow-cidr` with `--from`,
    so `force_allowlist` opens the state DB directly and sets
    the row's `allowed_cidrs` field. Round-trip tests against a
    real SQLite DB to lock down the BLOB encoding."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="smolvm-db.")
        self.db = Path(self._tmp.name) / "smolvm.db"
        con = sqlite3.connect(str(self.db))
        con.execute(
            "CREATE TABLE vms (name TEXT PRIMARY KEY NOT NULL, data BLOB NOT NULL)"
        )
        # Mimic smolvm's row shape (the JSON keys that exist on
        # creation; allowed_cidrs is the field we patch).
        cfg = {
            "name": "demo-vm",
            "cpus": 4,
            "mem": 8192,
            "network": True,
            "allowed_cidrs": None,
        }
        con.execute(
            "INSERT INTO vms (name, data) VALUES (?, ?)",
            ("demo-vm", sqlite3.Binary(json.dumps(cfg).encode())),
        )
        con.commit()
        con.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_patches_allowed_cidrs_on_row(self):
        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(loopback_alias, "_SMOLVM_DB_PATH", self.db):
            loopback_alias.force_allowlist("demo-vm", ["127.0.0.16/32"])

        con = sqlite3.connect(str(self.db))
        row = con.execute(
            "SELECT typeof(data), data FROM vms WHERE name='demo-vm'",
        ).fetchone()
        con.close()
        # Must round-trip as BLOB (the column type smolvm reads).
        self.assertEqual("blob", row[0])
        cfg = json.loads(row[1])
        self.assertEqual(["127.0.0.16/32"], cfg["allowed_cidrs"])
        # Other fields preserved verbatim.
        self.assertEqual(4, cfg["cpus"])
        self.assertTrue(cfg["network"])

    def test_noop_on_linux(self):
        with patch.object(loopback_alias, "_is_macos", return_value=False), \
             patch.object(loopback_alias, "_SMOLVM_DB_PATH", self.db):
            loopback_alias.force_allowlist("demo-vm", ["127.0.0.16/32"])
        # DB row should be untouched.
        con = sqlite3.connect(str(self.db))
        cfg = json.loads(con.execute(
            "SELECT data FROM vms WHERE name='demo-vm'",
        ).fetchone()[0])
        con.close()
        self.assertIsNone(cfg["allowed_cidrs"])

    def test_dies_on_missing_db(self):
        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(
                 loopback_alias, "_SMOLVM_DB_PATH",
                 Path("/nonexistent/smolvm.db"),
             ), patch.object(
                 loopback_alias, "die", side_effect=SystemExit("die"),
             ):
            with self.assertRaises(SystemExit):
                loopback_alias.force_allowlist("demo-vm", ["127.0.0.16/32"])

    def test_dies_on_missing_row(self):
        with patch.object(loopback_alias, "_is_macos", return_value=True), \
             patch.object(loopback_alias, "_SMOLVM_DB_PATH", self.db), \
             patch.object(
                 loopback_alias, "die", side_effect=SystemExit("die"),
             ):
            with self.assertRaises(SystemExit):
                loopback_alias.force_allowlist("not-in-db", ["127.0.0.16/32"])


if __name__ == "__main__":
    unittest.main()
