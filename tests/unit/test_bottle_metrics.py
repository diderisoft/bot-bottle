"""Unit: live CPU and memory metrics collection per bottle."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from bot_bottle.control.hostctl import bottle_metrics
from bot_bottle.control.hostctl.bottle_metrics import (
    BottleMetrics,
    _delta_cpu_pct,
    _parse_size,
    _parse_stats,
    _proc_jiffies,
    _proc_mem_bytes,
    _run_stats,
    collect,
)


# --- _proc_mem_bytes ---


class TestProcMemBytes(unittest.TestCase):
    def test_parses_vmrss_line(self) -> None:
        content = "Name:\tpython\nVmRSS:\t 65536 kB\nVmPeak:\t100000 kB\n"
        with mock.patch("pathlib.Path.read_text", return_value=content):
            self.assertEqual(65536 * 1024, _proc_mem_bytes(1))

    def test_returns_zero_when_proc_missing(self) -> None:
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("gone")):
            self.assertEqual(0, _proc_mem_bytes(1))

    def test_returns_zero_when_vmrss_absent(self) -> None:
        with mock.patch("pathlib.Path.read_text", return_value="Name:\tpython\n"):
            self.assertEqual(0, _proc_mem_bytes(1))

    def test_returns_zero_on_malformed_vmrss(self) -> None:
        with mock.patch("pathlib.Path.read_text", return_value="VmRSS:\tbad\n"):
            self.assertEqual(0, _proc_mem_bytes(1))


# --- _proc_jiffies ---


class TestProcJiffies(unittest.TestCase):
    # Format: pid (comm) state ... utime(14th field after ')') stime(15th)
    # fields after ')': [state, ppid, pgrp, session, tty, tpgid, flags,
    #                    minflt, cminflt, majflt, cmajflt, utime, stime ...]
    #                    indices 0..12 so utime=fields[11], stime=fields[12]
    _STAT = "1234 (bash) S 1 1 1 1 -1 0 0 0 0 0 100 50 0 0\n"

    def test_returns_utime_plus_stime(self) -> None:
        with mock.patch("pathlib.Path.read_text", return_value=self._STAT):
            self.assertEqual(150, _proc_jiffies(1))

    def test_returns_zero_on_oserror(self) -> None:
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            self.assertEqual(0, _proc_jiffies(1))

    def test_returns_zero_on_malformed_stat(self) -> None:
        with mock.patch("pathlib.Path.read_text", return_value="bad content"):
            self.assertEqual(0, _proc_jiffies(1))

    def test_handles_comm_with_spaces(self) -> None:
        stat = "1 (my proc) S 0 0 0 0 0 0 0 0 0 0 200 100 0 0\n"
        with mock.patch("pathlib.Path.read_text", return_value=stat):
            self.assertEqual(300, _proc_jiffies(1))


# --- _delta_cpu_pct ---


class TestDeltaCpuPct(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the cache before each test to avoid inter-test pollution.
        bottle_metrics._cpu_cache.clear()

    def test_first_call_returns_zero(self) -> None:
        with mock.patch("pathlib.Path.read_text", return_value="bad"), \
             mock.patch("time.monotonic", return_value=1000.0):
            result = _delta_cpu_pct("slug-a", 1)
        self.assertEqual(0.0, result)

    def test_second_call_returns_delta(self) -> None:
        stat1 = "1 (x) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"   # jiffies=0
        stat2 = "1 (x) S 0 0 0 0 0 0 0 0 0 0 100 0 0 0\n"  # utime=100

        times = iter([1000.0, 1001.0])
        stats = iter([stat1, stat2])

        with mock.patch("pathlib.Path.read_text", side_effect=lambda *a, **kw: next(stats)), \  # type: ignore[misc]
             mock.patch("time.monotonic", side_effect=lambda: next(times)), \
             mock.patch("os.sysconf", return_value=100):
            _delta_cpu_pct("slug-b", 1)   # first call
            pct = _delta_cpu_pct("slug-b", 1)  # second call: 100 jiffies / 100 Hz / 1s = 100%
        self.assertAlmostEqual(100.0, pct, places=1)

    def test_caps_at_100_percent(self) -> None:
        stat_zero = "1 (x) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        stat_huge = "1 (x) S 0 0 0 0 0 0 0 0 0 0 99999 0 0 0\n"

        times = iter([1000.0, 1000.001])  # tiny elapsed
        stats = iter([stat_zero, stat_huge])

        with mock.patch("pathlib.Path.read_text", side_effect=lambda *a, **kw: next(stats)), \  # type: ignore[misc]
             mock.patch("time.monotonic", side_effect=lambda: next(times)), \
             mock.patch("os.sysconf", return_value=100):
            _delta_cpu_pct("slug-c", 1)
            pct = _delta_cpu_pct("slug-c", 1)
        self.assertLessEqual(pct, 100.0)


# --- _parse_size ---


class TestParseSize(unittest.TestCase):
    def test_gib(self) -> None:
        self.assertEqual(2 * (1 << 30), _parse_size("2GiB"))

    def test_mib(self) -> None:
        self.assertEqual(512 * (1 << 20), _parse_size("512MiB"))

    def test_kib(self) -> None:
        self.assertEqual(1024 * (1 << 10), _parse_size("1024KiB"))

    def test_bytes_suffix(self) -> None:
        self.assertEqual(1024, _parse_size("1024B"))

    def test_decimal_si_units(self) -> None:
        self.assertEqual(1_000_000_000, _parse_size("1GB"))
        self.assertEqual(1_000_000, _parse_size("1MB"))

    def test_fractional_value(self) -> None:
        self.assertEqual(int(1.5 * (1 << 30)), _parse_size("1.5GiB"))

    def test_unknown_suffix_returns_zero(self) -> None:
        self.assertEqual(0, _parse_size("128TB"))

    def test_whitespace_stripped(self) -> None:
        self.assertEqual(1 << 20, _parse_size("  1MiB  "))

    def test_bad_number_returns_zero(self) -> None:
        self.assertEqual(0, _parse_size("xMiB"))


# --- _run_stats ---


class TestRunStats(unittest.TestCase):
    def _result(self, returncode: int = 0, stdout: str = '{"CPUPerc": "1%"}') -> mock.MagicMock:
        r = mock.MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        return r

    def test_returns_parsed_json_on_success(self) -> None:
        with mock.patch("subprocess.run", return_value=self._result()):
            data = _run_stats(["docker", "stats"])
        self.assertEqual({"CPUPerc": "1%"}, data)

    def test_returns_none_on_nonzero_exit(self) -> None:
        with mock.patch("subprocess.run", return_value=self._result(returncode=1)):
            self.assertIsNone(_run_stats(["docker", "stats"]))

    def test_returns_none_on_oserror(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("not found")):
            self.assertIsNone(_run_stats(["docker", "stats"]))

    def test_returns_none_on_timeout(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("d", 5)):
            self.assertIsNone(_run_stats(["docker", "stats"]))

    def test_returns_none_on_invalid_json(self) -> None:
        with mock.patch("subprocess.run", return_value=self._result(stdout="not json")):
            self.assertIsNone(_run_stats(["docker", "stats"]))

    def test_returns_none_when_stdout_empty(self) -> None:
        with mock.patch("subprocess.run", return_value=self._result(stdout="  ")):
            self.assertIsNone(_run_stats(["docker", "stats"]))

    def test_returns_none_when_json_is_a_list(self) -> None:
        with mock.patch("subprocess.run", return_value=self._result(stdout="[]")):
            self.assertIsNone(_run_stats(["docker", "stats"]))


# --- _parse_stats ---


class TestParseStats(unittest.TestCase):
    def test_docker_format(self) -> None:
        data: dict[str, object] = {"CPUPerc": "12.5%", "MemUsage": "256MiB / 1GiB"}
        m = _parse_stats(data)
        self.assertAlmostEqual(12.5, m.cpu_pct)
        self.assertEqual(256 * (1 << 20), m.mem_used)

    def test_missing_fields_default_to_zero(self) -> None:
        m = _parse_stats({})
        self.assertEqual(0.0, m.cpu_pct)
        self.assertEqual(0, m.mem_used)

    def test_mem_without_slash_uses_whole_string(self) -> None:
        m = _parse_stats({"MemUsage": "512MiB"})
        self.assertEqual(512 * (1 << 20), m.mem_used)

    def test_bad_cpu_string_defaults_to_zero(self) -> None:
        m = _parse_stats({"CPUPerc": "N/A"})
        self.assertEqual(0.0, m.cpu_pct)


# --- _metrics_firecracker ---


class TestMetricsFirecracker(unittest.TestCase):
    def test_returns_none_when_no_pid(self) -> None:
        with mock.patch(
            "bot_bottle.backend.firecracker.cleanup.pid_for_slug", return_value=None
        ):
            from bot_bottle.control.hostctl.bottle_metrics import _metrics_firecracker
            self.assertIsNone(_metrics_firecracker("slug-x"))

    def test_returns_metrics_when_pid_found(self) -> None:
        status = "Name:\tfc\nVmRSS:\t 65536 kB\n"
        stat = "1 (fc) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        bottle_metrics._cpu_cache.clear()

        def read_text_by_path(self_path: object, **_kw: object) -> str:
            path_str = str(self_path)
            if path_str.endswith("/status"):
                return status
            return stat

        with mock.patch("bot_bottle.backend.firecracker.cleanup.pid_for_slug", return_value=42), \
             mock.patch("pathlib.Path.read_text", read_text_by_path):
            from bot_bottle.control.hostctl.bottle_metrics import _metrics_firecracker
            result = _metrics_firecracker("slug-y")
        assert result is not None
        self.assertEqual(65536 * 1024, result.mem_used)


# --- _metrics_docker / _metrics_macos_container ---


class TestMetricsDockerAndMacOS(unittest.TestCase):
    def _stats_result(self, cpu: str = "5%", mem: str = "128MiB / 1GiB") -> mock.MagicMock:
        r = mock.MagicMock()
        r.returncode = 0
        r.stdout = f'{{"CPUPerc": "{cpu}", "MemUsage": "{mem}"}}'
        return r

    def test_docker_metrics_parsed(self) -> None:
        from bot_bottle.control.hostctl.bottle_metrics import _metrics_docker
        with mock.patch("subprocess.run", return_value=self._stats_result()), \
             mock.patch("bot_bottle.environment.resource_prefix", return_value="bb-"):
            result = _metrics_docker("myslug")
        assert result is not None
        self.assertAlmostEqual(5.0, result.cpu_pct)
        self.assertEqual(128 * (1 << 20), result.mem_used)

    def test_docker_returns_none_on_missing_container(self) -> None:
        from bot_bottle.control.hostctl.bottle_metrics import _metrics_docker
        r = mock.MagicMock()
        r.returncode = 1
        r.stdout = ""
        with mock.patch("subprocess.run", return_value=r), \
             mock.patch("bot_bottle.environment.resource_prefix", return_value="bb-"):
            self.assertIsNone(_metrics_docker("gone"))

    def test_macos_container_metrics_parsed(self) -> None:
        from bot_bottle.control.hostctl.bottle_metrics import _metrics_macos_container
        with mock.patch("subprocess.run", return_value=self._stats_result()), \
             mock.patch("bot_bottle.environment.resource_prefix", return_value="bb-"):
            result = _metrics_macos_container("myslug")
        assert result is not None
        self.assertAlmostEqual(5.0, result.cpu_pct)


# --- collect ---


class TestCollect(unittest.TestCase):
    def test_dispatches_to_docker(self) -> None:
        fake = BottleMetrics(cpu_pct=3.0, mem_used=1024)
        with mock.patch.dict(bottle_metrics._COLLECTORS, {"docker": lambda _s: fake}):  # type: ignore[misc]
            result = collect("s", "docker")
        self.assertEqual(fake, result)

    def test_unsupported_backend_returns_none(self) -> None:
        self.assertIsNone(collect("s", "unknown_backend"))

    def test_dispatches_to_firecracker(self) -> None:
        fake = BottleMetrics(cpu_pct=10.0, mem_used=2048)
        with mock.patch.dict(bottle_metrics._COLLECTORS, {"firecracker": lambda _s: fake}):  # type: ignore[misc]
            result = collect("s", "firecracker")
        self.assertEqual(fake, result)


if __name__ == "__main__":
    unittest.main()
