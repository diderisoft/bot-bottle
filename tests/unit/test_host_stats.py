"""Unit: host storage and memory statistics collection."""

from __future__ import annotations

import platform
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.control.hostctl import host_stats


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
            used, total = host_stats._memory_stats_macos()
        self.assertEqual(17179869184, total)

    def test_available_is_free_plus_inactive_pages(self) -> None:
        # free=262144, inactive=131072 → 393216 pages × 4096 = 1610612736 bytes
        with mock.patch("subprocess.run", side_effect=self._run):
            used, total = host_stats._memory_stats_macos()
        available = (262144 + 131072) * 4096
        self.assertEqual(total - available, used)


class TestCollect(unittest.TestCase):
    def test_returns_storage_and_memory_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect(Path(tmp))
        self.assertIn("storage", result)
        self.assertIn("memory", result)
        for section in ("storage", "memory"):
            for key in ("used", "total", "cap"):
                self.assertIn(key, result[section])  # type: ignore[index]

    def test_cap_is_zero_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect(Path(tmp))
        self.assertEqual(0, result["storage"]["cap"])  # type: ignore[index]
        self.assertEqual(0, result["memory"]["cap"])  # type: ignore[index]

    def test_cap_converts_gb_to_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect(Path(tmp), max_disk_gb=10, max_memory_gb=4)
        self.assertEqual(10 * 1024 ** 3, result["storage"]["cap"])  # type: ignore[index]
        self.assertEqual(4 * 1024 ** 3, result["memory"]["cap"])  # type: ignore[index]

    def test_disk_stats_are_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = host_stats.collect(Path(tmp))
        self.assertGreater(result["storage"]["total"], 0)  # type: ignore[index]
        self.assertGreaterEqual(result["storage"]["used"], 0)  # type: ignore[index]


class TestConsoleLink(unittest.TestCase):
    """push_host_stats sends the right message shape."""

    def test_push_host_stats_sends_host_stats_message(self) -> None:
        import asyncio
        from bot_bottle.control.hostctl import remote

        sent: list[dict] = []

        async def send(msg: dict) -> None:
            sent.append(msg)

        class _Bottles:
            def list(self) -> list:
                return []

        link = remote.ConsoleLink(_Bottles(), send)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("bot_bottle.control.hostctl.remote.bot_bottle_root",
                            return_value=Path(tmp)):
                asyncio.run(link.push_host_stats())

        self.assertEqual(1, len(sent))
        msg = sent[0]
        self.assertEqual("host_stats", msg["type"])
        self.assertIn("storage", msg)
        self.assertIn("memory", msg)
        for section in ("storage", "memory"):
            for key in ("used", "total", "cap"):
                self.assertIn(key, msg[section])

    def test_push_host_stats_uses_infra_caps(self) -> None:
        import asyncio
        from bot_bottle.contract.hostctl import InfraSettings
        from bot_bottle.control.hostctl import remote

        sent: list[dict] = []

        async def send(msg: dict) -> None:
            sent.append(msg)

        class _Bottles:
            def list(self) -> list:
                return []

        infra = InfraSettings(host_max_disk_gb=20, host_max_memory_gb=8)
        link = remote.ConsoleLink(_Bottles(), send, infra=infra)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("bot_bottle.control.hostctl.remote.bot_bottle_root",
                            return_value=Path(tmp)):
                asyncio.run(link.push_host_stats())

        msg = sent[0]
        self.assertEqual(20 * 1024 ** 3, msg["storage"]["cap"])
        self.assertEqual(8 * 1024 ** 3, msg["memory"]["cap"])
