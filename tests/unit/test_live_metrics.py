"""Unit: the console's live resource gauge (`metrics_watch` -> `bottle_metrics`)."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.backend.bottle_size import BOTTLE_MEMORY_BYTES
from bot_bottle.backend.firecracker.cleanup import vcpus_for_slug
from bot_bottle.control.hostctl import bottle_metrics, live_metrics, remote
from bot_bottle.control.hostctl.bottle_metrics import BottleMetrics, collect
from bot_bottle.control.hostctl.bottles import BottleService
from bot_bottle.control.hostctl.live_metrics import MAX_WATCHED, MetricsWatch, watched


def _entry(bottle: str) -> dict[str, object]:
    return {"id": bottle, "cpu_pct": 12.5, "mem_used": 1024, "memory": BOTTLE_MEMORY_BYTES}


class _Sink:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


class TestWatched(unittest.TestCase):
    def test_keeps_ids_once_in_order(self) -> None:
        self.assertEqual(["a", "b"], watched(["a", "b", "a"]))

    def test_drops_what_isnt_an_id(self) -> None:
        self.assertEqual(["a"], watched(["a", "", 3, None, "x" * 201]))
        self.assertEqual([], watched("a"))
        self.assertEqual([], watched(None))

    def test_is_capped(self) -> None:
        self.assertEqual(MAX_WATCHED, len(watched([f"b{i}" for i in range(MAX_WATCHED + 5)])))


class TestTick(unittest.TestCase):
    def setUp(self) -> None:
        self.sink = _Sink()
        self.running = {"a", "b"}
        self.sampled: list[str] = []

    def sample(self, bottle: str) -> dict[str, object] | None:
        self.sampled.append(bottle)
        return _entry(bottle)

    def watch(self, sample: Any = None) -> MetricsWatch:
        return MetricsWatch(self.sink, lambda: self.running,
                            self.sample if sample is None else sample)

    def test_sends_the_watched_running_bottles_in_one_message(self) -> None:
        w = self.watch()
        w.watch(["a", "gone", "b"])
        self.assertTrue(asyncio.run(w.tick()))
        self.assertEqual(["a", "b"], self.sampled)
        self.assertEqual(1, len(self.sink.sent))
        msg = self.sink.sent[0]
        self.assertEqual("bottle_metrics", msg["type"])
        self.assertEqual([_entry("a"), _entry("b")], msg["bottles"])
        self.assertIn("at", msg)

    def test_nothing_watched_or_running_sends_nothing(self) -> None:
        w = self.watch()
        self.assertFalse(asyncio.run(w.tick()))
        w.watch(["gone"])
        self.assertFalse(asyncio.run(w.tick()))
        self.assertEqual([], self.sink.sent)

    def test_an_empty_watch_stops(self) -> None:
        w = self.watch()
        w.watch(["a"])
        w.watch([])
        self.assertFalse(asyncio.run(w.tick()))
        self.assertEqual([], w.bottles)

    def test_a_host_that_cant_sample_sends_nothing(self) -> None:
        w = MetricsWatch(self.sink, lambda: self.running, None)
        w.watch(["a"])
        self.assertFalse(asyncio.run(w.tick()))

    def test_an_unreadable_bottle_is_left_out(self) -> None:
        def sample(bottle: str) -> dict[str, object] | None:
            if bottle == "a":
                raise OSError("gone")
            return None if bottle == "b" else _entry(bottle)

        self.running = {"a", "b", "c"}
        w = self.watch(sample)
        w.watch(["a", "b", "c"])
        self.assertTrue(asyncio.run(w.tick()))
        self.assertEqual([_entry("c")], self.sink.sent[0]["bottles"])
        w.watch(["a", "b"])
        self.assertFalse(asyncio.run(w.tick()))

    def test_a_pass_still_running_skips_the_next(self) -> None:
        started, release = threading.Event(), threading.Event()

        def slow(bottle: str) -> dict[str, object] | None:
            started.set()
            release.wait(5)
            return _entry(bottle)

        w = self.watch(slow)
        w.watch(["a"])

        async def go() -> tuple[bool, bool]:
            first = asyncio.ensure_future(w.tick())
            await asyncio.to_thread(started.wait, 5)
            second = await w.tick()  # the first is mid-pass
            release.set()
            return await first, second

        self.assertEqual((True, False), asyncio.run(go()))
        self.assertEqual(1, len(self.sink.sent))


class TestRun(unittest.TestCase):
    def test_ticks_on_the_interval_while_watched(self) -> None:
        sink = _Sink()
        w = MetricsWatch(sink, lambda: {"a"}, _entry)

        async def go() -> None:
            task = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.05)
            self.assertEqual([], sink.sent)  # nothing watched yet
            w.watch(["a"])
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.object(live_metrics, "LIVE_METRICS_INTERVAL_SECONDS", 0.01):
            asyncio.run(go())
        self.assertGreaterEqual(len(sink.sent), 2)

    def test_a_failed_send_is_logged_and_the_loop_goes_on(self) -> None:
        calls = 0

        async def failing(_message: dict[str, Any]) -> None:
            nonlocal calls
            calls += 1
            raise ConnectionError("link down")

        w = MetricsWatch(failing, lambda: {"a"}, _entry)
        w.watch(["a"])

        async def go() -> None:
            task = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.object(live_metrics, "LIVE_METRICS_INTERVAL_SECONDS", 0.01):
            asyncio.run(go())
        self.assertGreaterEqual(calls, 2)

    def test_the_interval_is_three_seconds(self) -> None:
        self.assertEqual(3.0, live_metrics.LIVE_METRICS_INTERVAL_SECONDS)


class TestLink(unittest.TestCase):
    def test_metrics_watch_sets_the_links_watched_bottles(self) -> None:
        bottles = SimpleNamespace(live_metrics=_entry)

        async def send(_message: dict[str, Any]) -> None:
            return None

        link = remote.ConsoleLink(bottles, send, agents=lambda: [])  # type: ignore[arg-type]
        link.running["agent-1"] = "a"
        asyncio.run(link.handle({"type": "metrics_watch", "bottles": ["a", "z"]}))
        self.assertEqual(["a", "z"], link.metrics.bottles)
        sink = _Sink()
        link.metrics._send = sink  # pylint: disable=protected-access
        asyncio.run(link.metrics.tick())
        self.assertEqual([_entry("a")], sink.sent[0]["bottles"])  # only what it runs


class TestServiceSample(unittest.TestCase):
    def service(self, record: Any) -> BottleService:
        manager = mock.Mock()
        manager.record.return_value = record
        return BottleService(manager)

    def test_reads_the_bottle_on_its_own_baseline(self) -> None:
        record = SimpleNamespace(id="a", meta={"backend": "firecracker"})
        with mock.patch.object(bottle_metrics, "collect",
                               return_value=BottleMetrics(cpu_pct=40.0, mem_used=99)) as got:
            self.assertEqual({"id": "a", "cpu_pct": 40.0, "mem_used": 99,
                              "memory": BOTTLE_MEMORY_BYTES},
                             self.service(record).live_metrics("a"))
        got.assert_called_once_with("a", "firecracker", sample="live")

    def test_none_when_unknown_or_unreadable(self) -> None:
        self.assertIsNone(self.service(None).live_metrics("a"))
        record = SimpleNamespace(id="a", meta={})
        with mock.patch.object(bottle_metrics, "collect", return_value=None):
            self.assertIsNone(self.service(record).live_metrics("a"))


class TestSampleBaselines(unittest.TestCase):
    """The live gauge and `host_stats` each measure CPU from their own last sample."""

    def setUp(self) -> None:
        bottle_metrics._cpu_cache.clear()  # pylint: disable=protected-access
        self.addCleanup(bottle_metrics._cpu_cache.clear)  # pylint: disable=protected-access

    def test_a_live_sample_doesnt_move_the_minute_baseline(self) -> None:
        clock = iter([0.0, 1.0, 2.0, 60.0])
        jiffies = iter([0, 100, 200, 6000])

        def next_jiffies(_pid: int) -> int:
            return next(jiffies)

        with mock.patch("bot_bottle.backend.firecracker.cleanup.pid_for_slug", return_value=7), \
             mock.patch.object(bottle_metrics.time, "monotonic", lambda: next(clock)), \
             mock.patch.object(bottle_metrics, "_proc_jiffies", next_jiffies), \
             mock.patch.object(bottle_metrics, "_proc_mem_bytes", return_value=1), \
             mock.patch.object(bottle_metrics.os, "sysconf", return_value=100):
            first = collect("a", "firecracker")                  # minute: t=0, j=0
            collect("a", "firecracker", sample="live")           # live: t=1, j=100
            live = collect("a", "firecracker", sample="live")    # live: t=2, j=200
            minute = collect("a", "firecracker")                 # minute: t=60, j=6000
        assert first is not None and live is not None and minute is not None
        self.assertEqual(0.0, first.cpu_pct)
        self.assertAlmostEqual(100.0, live.cpu_pct)      # 100 jiffies in 1 s
        self.assertAlmostEqual(100.0, minute.cpu_pct)    # 6000 jiffies in 60 s, not 5800 in 58


if __name__ == "__main__":
    unittest.main()


class TestCpuShare(unittest.TestCase):
    """CPU % is a share of the bottle's CPUs, capped at 100."""

    def setUp(self) -> None:
        bottle_metrics._cpu_cache.clear()  # pylint: disable=protected-access
        self.addCleanup(bottle_metrics._cpu_cache.clear)  # pylint: disable=protected-access

    def test_one_busy_vcpu_of_two_is_half(self) -> None:
        clock = iter([0.0, 1.0])
        jiffies = iter([0, 100])

        def next_jiffies(_pid: int) -> int:
            return next(jiffies)

        with mock.patch("bot_bottle.backend.firecracker.cleanup.pid_for_slug", return_value=7), \
             mock.patch("bot_bottle.backend.firecracker.cleanup.vcpus_for_slug", return_value=2), \
             mock.patch.object(bottle_metrics.time, "monotonic", lambda: next(clock)), \
             mock.patch.object(bottle_metrics, "_proc_jiffies", next_jiffies), \
             mock.patch.object(bottle_metrics, "_proc_mem_bytes", return_value=1), \
             mock.patch.object(bottle_metrics.os, "sysconf", return_value=100):
            collect("a", "firecracker")
            got = collect("a", "firecracker")
        assert got is not None
        self.assertAlmostEqual(50.0, got.cpu_pct)

    def test_both_vcpus_busy_is_full_not_more(self) -> None:
        clock = iter([0.0, 1.0])
        jiffies = iter([0, 260])  # more than two cores' worth: still 100

        def next_jiffies(_pid: int) -> int:
            return next(jiffies)

        with mock.patch.object(bottle_metrics.time, "monotonic", lambda: next(clock)), \
             mock.patch.object(bottle_metrics, "_proc_jiffies", next_jiffies), \
             mock.patch.object(bottle_metrics.os, "sysconf", return_value=100):
            bottle_metrics._delta_cpu_pct("b", 7, 2)  # pylint: disable=protected-access
            pct = bottle_metrics._delta_cpu_pct("b", 7, 2)  # pylint: disable=protected-access
        self.assertEqual(100.0, pct)

    def test_container_stats_are_capped(self) -> None:
        parse = bottle_metrics._parse_stats  # pylint: disable=protected-access
        self.assertEqual(100.0, parse({"CPUPerc": "187.5%"}).cpu_pct)
        self.assertEqual(0.0, parse({"CPUPerc": "-3%"}).cpu_pct)
        self.assertEqual(12.5, parse({"CPUPerc": "12.5%"}).cpu_pct)


class TestVcpus(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(
            tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.enterContext(mock.patch("bot_bottle.backend.firecracker.cleanup._run_root",
                                     return_value=self.root))

    def write(self, slug: str, text: str) -> None:
        (self.root / slug).mkdir()
        (self.root / slug / "config.json").write_text(text)

    def test_reads_the_vms_config(self) -> None:
        self.write("a", '{"machine-config": {"vcpu_count": 2, "mem_size_mib": 2048}}')
        self.assertEqual(2, vcpus_for_slug("a"))

    def test_one_when_unreadable(self) -> None:
        self.assertEqual(1, vcpus_for_slug("missing"))
        for slug, text in (("bad", "{"), ("none", "{}"), ("list", "[]"),
                           ("zero", '{"machine-config": {"vcpu_count": 0}}'),
                           ("flag", '{"machine-config": {"vcpu_count": true}}')):
            self.write(slug, text)
            self.assertEqual(1, vcpus_for_slug(slug), slug)
