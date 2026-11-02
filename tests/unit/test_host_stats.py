"""Unit: host storage and memory statistics collection."""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import budget, host_stats
from bot_bottle.lib.config_store import ConfigStore, HostBudget


class TestDiskStats(unittest.TestCase):
    def test_reports_used_and_total_for_a_real_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            used, total = host_stats._disk_stats(Path(tmp))
        self.assertGreater(total, 0)
        self.assertGreaterEqual(used, 0)
        self.assertLessEqual(used, total)

    def test_returns_zeros_for_a_missing_path(self) -> None:
        self.assertEqual((0, 0), host_stats._disk_stats(Path("/does/not/exist")))


class TestMemoryStatsLinux(unittest.TestCase):
    _MEMINFO = (
        "MemTotal:        4096000 kB\n"
        "MemFree:          512000 kB\n"
        "MemAvailable:    1024000 kB\n"
        "Buffers:          128000 kB\n"
    )

    def test_used_is_total_minus_available(self) -> None:
        with mock.patch("builtins.open", mock.mock_open(read_data=self._MEMINFO)):
            used, total = host_stats._memory_stats_linux()
        self.assertEqual(4096000 * 1024, total)
        self.assertEqual((4096000 - 1024000) * 1024, used)

    def test_returns_zeros_when_proc_meminfo_is_missing(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError):
            used, total = host_stats._memory_stats_linux()
        self.assertEqual((0, 0), (used, total))

    def test_returns_zeros_on_malformed_content(self) -> None:
        with mock.patch("builtins.open", mock.mock_open(read_data="MemTotal: garbage kB\n")):
            used, total = host_stats._memory_stats_linux()
        self.assertEqual((0, 0), (used, total))


class TestMemoryStatsMacOS(unittest.TestCase):
    _SYSCTL_OUT = "17179869184\n"  # 16 GiB
    _VM_STAT_OUT = (
        "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
        "Pages free:                          262144.\n"
        "Pages active:                        524288.\n"
        "Pages inactive:                      131072.\n"
        "Pages wired down:                    262144.\n"
    )

    def _run(self, cmd: list[str], **_kw: object) -> mock.MagicMock:
        result = mock.MagicMock()
        result.returncode = 0
        if cmd[0] == "sysctl":
            result.stdout = self._SYSCTL_OUT
        else:
            result.stdout = self._VM_STAT_OUT
        return result

    def test_total_comes_from_sysctl(self) -> None:
        with mock.patch("subprocess.run", side_effect=self._run):
            _, total = host_stats._memory_stats_macos()
        self.assertEqual(17179869184, total)

    def test_available_is_free_plus_inactive_pages(self) -> None:
        # free=262144, inactive=131072 → 393216 pages × 4096 = 1610612736 bytes
        with mock.patch("subprocess.run", side_effect=self._run):
            used, total = host_stats._memory_stats_macos()
        available = (262144 + 131072) * 4096
        self.assertEqual(total - available, used)


class TestCollect(unittest.TestCase):
    def test_returns_storage_and_memory_with_used_total_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect([Path(tmp)])
        for section in ("storage", "memory"):
            self.assertEqual({"used", "total", "cap"}, set(result[section]))  # type: ignore[arg-type]

    def test_cap_is_zero_when_not_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect([Path(tmp)])
        self.assertEqual(0, result["storage"]["cap"])  # type: ignore[index]
        self.assertEqual(0, result["memory"]["cap"])  # type: ignore[index]

    def test_cap_converts_gib_to_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect([Path(tmp)], limits=HostBudget(disk_gb=10, memory_gb=4))
        self.assertEqual(10 * 1024 ** 3, result["storage"]["cap"])  # type: ignore[index]
        self.assertEqual(4 * 1024 ** 3, result["memory"]["cap"])  # type: ignore[index]

    def test_disk_stats_are_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect([Path(tmp)])
        self.assertGreater(result["storage"]["total"], 0)  # type: ignore[index]
        self.assertGreaterEqual(result["storage"]["used"], 0)  # type: ignore[index]


class TestStorage(unittest.TestCase):
    """Storage covers the filesystems holding the root and the caches, each
    filesystem once."""

    def test_one_filesystem_is_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            a.mkdir()
            b.mkdir()
            self.assertEqual(host_stats._disk_stats(a), host_stats._storage([a, b]))

    def test_two_filesystems_add_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            a.mkdir()
            b.mkdir()
            devices = {a: 1, b: 2}
            real_stat = os.stat
            def stat(path: object, *args: object, **kw: object) -> object:
                p = Path(str(path))
                if p in devices:
                    return SimpleNamespace(st_dev=devices[p])
                return real_stat(path, *args, **kw)  # type: ignore[arg-type]
            with mock.patch.object(host_stats.os, "stat", side_effect=stat), \
                 mock.patch.object(host_stats, "_disk_stats", return_value=(3, 10)):
                self.assertEqual((6, 20), host_stats._storage([a, b]))

    def test_a_path_not_made_yet_counts_its_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            later = Path(tmp) / "cache" / "firecracker"
            # Free space on a live filesystem moves between two reads; check
            # which filesystem is asked, not its numbers.
            with mock.patch.object(host_stats, "_disk_stats", return_value=(3, 10)) as disk:
                self.assertEqual((3, 10), host_stats._storage([later]))
            disk.assert_called_once_with(Path(tmp))

    def test_no_existing_path_is_zero(self) -> None:
        with mock.patch.object(host_stats, "_existing", return_value=None):
            self.assertEqual((0, 0), host_stats._storage([Path("/nowhere")]))

    def test_stats_paths_are_the_root_and_the_caches_and_make_nothing(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "BOT_BOTTLE_FC_CACHE"}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {**env, "HOME": tmp}, clear=True), \
             mock.patch("bot_bottle.paths.bot_bottle_root", return_value=Path(tmp) / "root"):
            paths = host_stats.stats_paths()
            self.assertEqual(Path(tmp) / "root", paths[0])
            self.assertIn(Path(tmp) / ".cache" / "bot-bottle" / "firecracker", paths)
            self.assertFalse((Path(tmp) / ".cache").exists())


class TestBudgetSetting(unittest.TestCase):
    """The budget comes from `bot-bottle host budget` (the host DB)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "db" / "bot-bottle.db"
        self.enterContext(mock.patch("bot_bottle.paths.host_db_path", return_value=self.db))

    def test_none_set_is_zero(self) -> None:
        self.assertEqual(HostBudget(), budget.host_budget())

    def test_stored_budget(self) -> None:
        store = ConfigStore(self.db)
        store.migrate()
        store.set_host_budget(HostBudget(disk_gb=50, memory_gb=16))
        self.assertEqual(HostBudget(50, 16), budget.host_budget())
        stats: Any = host_stats.snapshot()
        self.assertEqual(50 * 1024 ** 3, stats["storage"]["cap"])
        self.assertEqual(16 * 1024 ** 3, stats["budget"]["memory"]["budget"])


class TestHostBudgetCommand(TestBudgetSetting):
    def run_cmd(self, argv: list[str]) -> tuple[int, str, str]:
        from bot_bottle.cli.commands import host
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host.cmd_host(argv)
        return code, out.getvalue(), err.getvalue()

    def test_shows_none_then_sets_and_clears(self) -> None:
        each = "(each bottle is given 2 GB)"
        self.assertEqual((0, f"memory: none {each}\nstorage: none\n", ""),
                         self.run_cmd(["budget"]))
        code, out, _ = self.run_cmd(["budget", "--disk", "50", "--memory", "16"])
        self.assertEqual((0, f"memory: 16 GB {each}\nstorage: 50 GB\n"), (code, out))
        code, out, _ = self.run_cmd(["budget", "--memory", "0"])  # one at a time; 0 clears
        self.assertEqual(f"memory: none {each}\nstorage: 50 GB\n", out)

    def test_refuses_a_negative_budget(self) -> None:
        code, _, err = self.run_cmd(["budget", "--disk", "-1"])
        self.assertEqual(2, code)
        self.assertIn("0 or more", err)

    def test_caps_still_works_and_points_at_budget(self) -> None:
        code, out, err = self.run_cmd(["caps", "--memory", "8"])
        self.assertEqual(0, code)
        self.assertIn("memory: 8 GB", out)
        self.assertIn("host budget", err)


class TestConsoleLink(unittest.TestCase):
    """The link sends `host_stats` (storage and memory, used/total/cap) on
    connect and once a minute while connected."""

    def _link(self) -> tuple[Any, list[dict[str, Any]]]:
        from bot_bottle.control.hostctl import remote

        sent: list[dict[str, Any]] = []

        async def send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        class _Bottles:
            def list(self) -> list[Any]:
                return []

        return remote.ConsoleLink(_Bottles(), send), sent  # type: ignore[arg-type]

    def test_push_host_stats_sends_the_snapshot(self) -> None:
        link, sent = self._link()
        stats = {"storage": {"used": 1, "total": 2, "cap": 0},
                 "memory": {"used": 3, "total": 4, "cap": 5}}
        with mock.patch.object(host_stats, "snapshot", return_value=stats) as snapshot:
            asyncio.run(link.push_host_stats())
        self.assertEqual([{"type": "host_stats", **stats}], sent)
        # With the bottle service, for the budget's running bottles.
        snapshot.assert_called_once_with(link._bottles)

    def test_a_failed_snapshot_sends_nothing(self) -> None:
        link, sent = self._link()
        with mock.patch.object(host_stats, "snapshot", side_effect=OSError("boom")):
            asyncio.run(link.push_host_stats())
        self.assertEqual([], sent)

    def test_only_due_once_a_minute(self) -> None:
        from bot_bottle.control.hostctl import remote
        link, sent = self._link()
        stats = {"storage": {}, "memory": {}}
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[100.0, 100.0, 130.0, 161.0, 161.0]))
        with mock.patch.object(host_stats, "snapshot", return_value=stats), \
             mock.patch.object(remote, "time", clock):
            asyncio.run(link.push_host_stats_if_due())   # first: due (sent at 100)
            asyncio.run(link.push_host_stats_if_due())   # 30 s later: not due
            asyncio.run(link.push_host_stats_if_due())   # 61 s later: due
        self.assertEqual(2, len(sent))
