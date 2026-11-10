"""Unit: the bottles' budget: storage walk categories, launch admission, the
`budget` block of `host_stats`."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle.backend.bottle_size import BOTTLE_MEMORY_BYTES
from bot_bottle.backend.runtime_store import StoreUsage
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.control.hostctl import budget, host_stats, sessions
from bot_bottle.control.hostctl.bottles import SESSION_KIND, BottleService
from bot_bottle.lib.config_store import HostBudget
from bot_bottle.lib.log import Die

from . import use_bottle_root
from .test_hostctl_bottles import FakeBackend

GIB = 1024 ** 3


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _running(n: int) -> list[dict[str, object]]:
    return [{"id": f"b{i}", "label": f"bottle {i}", "memory": BOTTLE_MEMORY_BYTES}
            for i in range(n)]


class TestWalk(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(
            tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.state, self.cache = self.tmp / "root", self.tmp / "cache"

    def walk(self) -> dict[str, int]:
        return budget.walk(((budget.STATE, self.state), (budget.CACHE, self.cache)))

    def test_sorts_what_it_finds_into_categories(self) -> None:
        _write(self.state / "transcripts" / "a.jsonl", 4096)
        _write(self.state / "audit" / "x" / "j.log", 4096)
        _write(self.state / "state" / "demo" / "home.ext4", 8192)
        _write(self.state / "state" / "demo" / "Dockerfile", 4096)
        _write(self.state / "build-root" / "abc" / "Dockerfile", 4096)
        _write(self.cache / "run" / "demo" / "rootfs.ext4", 8192)
        _write(self.cache / "envs" / "dev" / "run" / "d" / "rootfs.ext4", 4096)
        _write(self.cache / "infra" / "gateway" / "rootfs.ext4", 4096)
        _write(self.cache / "infra" / "registry.ext4", 4096)
        _write(self.cache / "rootfs.squashfs", 4096)
        got = self.walk()
        self.assertEqual(8192, got[budget.TRANSCRIPTS])
        self.assertEqual(8192 + 8192 + 4096 + 4096, got[budget.VM_DISKS])
        self.assertEqual(4096, got[budget.WORKSPACES])
        self.assertEqual(4096 * 3, got[budget.IMAGES])

    def test_a_sparse_disk_counts_what_it_holds(self) -> None:
        disk = self.cache / "run" / "d" / "rootfs.ext4"
        disk.parent.mkdir(parents=True)
        with open(disk, "wb") as fh:
            fh.truncate(GIB)
        self.assertLess(self.walk()[budget.VM_DISKS], GIB // 100)

    def test_a_nested_root_and_a_hard_link_count_once(self) -> None:
        _write(self.cache / "a.bin", 4096)
        os.link(self.cache / "a.bin", self.cache / "b.bin")
        got = budget.walk(((budget.CACHE, self.tmp), (budget.CACHE, self.cache)))
        self.assertEqual(4096, got[budget.IMAGES])

    def test_missing_roots_and_symlinks_are_nothing(self) -> None:
        self.cache.mkdir()
        (self.cache / "loop").symlink_to(self.tmp)
        self.assertEqual(dict.fromkeys(budget.CATEGORIES, 0), self.walk())

    def test_storage_roots_are_the_state_root_and_the_caches(self) -> None:
        kinds = [kind for kind, _ in budget.storage_roots()]
        self.assertEqual([budget.STATE, budget.CACHE, budget.CACHE], kinds)


class TestStorageMeter(unittest.TestCase):
    def test_nothing_until_the_first_walk_then_the_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "x", 4096)
            meter = budget.StorageMeter(lambda: ((budget.CACHE, Path(tmp)),))
            self.assertIsNone(meter.current())
            meter.measure()
            self.assertEqual(4096, (meter.current() or {})[budget.IMAGES])

    def test_walks_in_its_own_thread_until_stopped(self) -> None:
        walked = threading.Event()

        def roots() -> tuple[()]:
            walked.set()
            return ()

        meter = budget.StorageMeter(roots, interval=0.01)
        meter.start()
        meter.start()  # once
        self.assertTrue(walked.wait(5))
        meter.stop()

    def test_adds_what_the_runtimes_hold_and_names_the_unmeasured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp) / "x", 4096)
            meter = budget.StorageMeter(
                lambda: ((budget.CACHE, Path(tmp)),),
                runtime=lambda: StoreUsage(images=GIB, vm_disks=2 * GIB,
                                           unknown=("container: not running",)))
            got = meter.measure()
        self.assertEqual(GIB + 4096, got[budget.IMAGES])
        self.assertEqual(2 * GIB, got[budget.VM_DISKS])
        self.assertEqual(("container: not running",), meter.unmeasured())

    def test_a_failed_walk_keeps_the_thread(self) -> None:
        calls: list[int] = []
        done = threading.Event()

        def roots() -> tuple[()]:
            calls.append(1)
            if len(calls) == 2:
                done.set()
            raise OSError("gone")

        meter = budget.StorageMeter(roots, interval=0.01)
        meter.start()
        self.assertTrue(done.wait(5))
        meter.stop()


class TestRefusal(unittest.TestCase):
    def test_no_budget_no_check(self) -> None:
        self.assertIsNone(budget.refusal("x", _running(50), HostBudget(), {"images": 10 * GIB}))

    def test_memory_fits_until_the_last_bottle(self) -> None:
        limits = HostBudget(memory_gb=8)
        self.assertIsNone(budget.refusal("x", _running(3), limits, None))
        self.assertEqual(
            "fly-deploy needs 2 GB of memory; 0 GB left in the 8 GB budget. Stop a bottle "
            "(`bot-bottle bots`) or raise the budget (`bot-bottle host budget --memory …`).",
            budget.refusal("fly-deploy", _running(4), limits, None))

    def test_partial_room_is_shown(self) -> None:
        msg = budget.refusal("x", _running(1), HostBudget(memory_gb=3), None) or ""
        self.assertIn("1 GB left in the 3 GB budget", msg)

    def test_storage_over_budget_names_cleanup(self) -> None:
        limits = HostBudget(disk_gb=10)
        self.assertIsNone(budget.refusal("x", [], limits, {"images": 10 * GIB}))
        self.assertIsNone(budget.refusal("x", [], limits, None))  # not walked yet
        msg = budget.refusal("x", [], limits, {"images": 9 * GIB, "vm_disks": 2 * GIB}) or ""
        self.assertIn("11 GB of storage, over the 10 GB budget", msg)
        self.assertIn("bot-bottle cleanup images", msg)

    def test_storage_warns_from_90_percent(self) -> None:
        limits = HostBudget(disk_gb=10)
        self.assertIsNone(budget.storage_warning(limits, {"images": 8 * GIB}))
        self.assertIsNone(budget.storage_warning(HostBudget(), {"images": 80 * GIB}))
        self.assertIsNone(budget.storage_warning(limits, None))
        self.assertIn("cleanup images", budget.storage_warning(limits, {"images": 9 * GIB}) or "")

    def test_gb(self) -> None:
        self.assertEqual(("2", "1.5", "0"), (budget.gb(2 * GIB), budget.gb(GIB * 3 // 2),
                                             budget.gb(0)))


class TestBlock(unittest.TestCase):
    def test_shape(self) -> None:
        got = budget.block(_running(2), HostBudget(memory_gb=8, disk_gb=300),
                           {"vm_disks": 1, "images": 2, "workspaces": 3, "transcripts": 4})
        self.assertEqual({
            "memory": {"allocated": 2 * BOTTLE_MEMORY_BYTES, "budget": 8 * GIB,
                       "bottle_size": BOTTLE_MEMORY_BYTES, "bottles": _running(2)},
            "storage": {"used": 10, "budget": 300 * GIB,
                        "categories": {"vm_disks": 1, "images": 2, "workspaces": 3,
                                       "transcripts": 4}},
        }, got)

    def test_storage_is_left_out_until_walked(self) -> None:
        self.assertNotIn("storage", budget.block([], HostBudget(), None))

    def test_names_the_runtimes_it_could_not_count(self) -> None:
        storage = budget.block([], HostBudget(), {"images": 1}, ("docker: no answer in 60s",))
        self.assertEqual(["docker: no answer in 60s"],
                         cast(dict[str, Any], storage["storage"])["unmeasured"])
        measured = budget.block([], HostBudget(), {"images": 1})["storage"]
        self.assertNotIn("unmeasured", cast(dict[str, Any], measured))

    def test_snapshot_carries_the_running_bottles(self) -> None:
        bottles = mock.Mock(allocation=mock.Mock(return_value=_running(1)))
        with mock.patch.object(budget, "host_budget", return_value=HostBudget(memory_gb=4)), \
             mock.patch.object(budget.METER, "current", return_value=None):
            stats: Any = host_stats.snapshot(bottles)
        self.assertEqual(BOTTLE_MEMORY_BYTES, stats["budget"]["memory"]["allocated"])
        self.assertEqual(4 * GIB, stats["memory"]["cap"])
        self.assertIn("used", stats["storage"])  # host-wide numbers kept


class TestAllocation(unittest.TestCase):
    """Running = held, adopted, launching, or with a running session."""

    def test_counts_every_running_kind_once(self) -> None:
        def record(i: str) -> Any:
            return SimpleNamespace(id=i, kind=SESSION_KIND, meta={})

        manager = mock.Mock()
        manager.records.return_value = [
            record("held"), record("adopted"), record("live"), record("ended"),
            SimpleNamespace(id="shell", kind="other", meta={})]
        def exit_status(bottle_id: str) -> object:
            return None if bottle_id == "live" else mock.Mock()

        manager.exit_status.side_effect = exit_status
        manager.exists.return_value = True
        service = BottleService(manager)
        service._held["held"] = cast(Any, object())
        service._adopted["adopted"] = lambda: None
        service._launching.update({"new": ("New one", BOTTLE_MEMORY_BYTES),
                                   "held": ("Held", BOTTLE_MEMORY_BYTES)})
        # The label is the bottled agent's, kept only in its row.
        labelled = [SimpleNamespace(identity="held", label="Held")]
        with mock.patch("bot_bottle.bottle_state.all_metadata", return_value=labelled):
            got = service.allocation()
        self.assertEqual(["held", "adopted", "live", "new"], [b["id"] for b in got])
        self.assertEqual(["Held", "adopted", "live", "New one"], [b["label"] for b in got])
        self.assertEqual({BOTTLE_MEMORY_BYTES}, {b["memory"] for b in got})


class TestAdmission(unittest.TestCase):
    """`BottleService._launch` refuses a launch the budget can't fit, after
    preparing and before booting."""

    def setUp(self) -> None:
        root = Path(self.enterContext(
            tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.backend = FakeBackend()
        self.limits = HostBudget()
        self.storage: dict[str, int] | None = None
        self.service = BottleService(
            sessions.SessionManager({}), backend_for=lambda _s: cast(Any, self.backend),
            manifest_for=lambda _cwd: mock.MagicMock(), secrets_file=root / "secrets",
            host_budget=lambda: self.limits, storage=lambda: self.storage)

    def launch(self, label: str) -> None:
        self.service.launch(LaunchRequest(agent="demo", label=label, assume_yes=True))

    def test_refused_when_memory_wont_fit(self) -> None:
        self.limits = HostBudget(memory_gb=1)
        with self.assertRaises(Die) as cm:
            self.launch("fly-deploy")
        self.assertIn("fly-deploy needs 2 GB of memory; 1 GB left in the 1 GB budget",
                      cm.exception.message)
        self.assertFalse(self.backend.launched.is_set())
        self.assertEqual({}, self.service._launching)

    def test_refused_when_storage_is_over(self) -> None:
        self.limits, self.storage = HostBudget(disk_gb=1), {"images": 2 * GIB}
        with self.assertRaises(Die) as cm:
            self.launch("big")
        self.assertIn("bot-bottle cleanup images", cm.exception.message)
        self.assertFalse(self.backend.launched.is_set())

    def test_admitted_launches_boot_and_stop_counting_as_launching(self) -> None:
        self.limits, self.storage = HostBudget(memory_gb=2, disk_gb=10), {"images": 9 * GIB}
        self.backend.fail_launch = True  # past admission, into the holder
        with mock.patch("bot_bottle.lib.log.warn") as warn, self.assertRaises(RuntimeError):
            self.launch("fits")
        self.assertIn("storage budget", warn.call_args.args[0])
        self.assertEqual({}, self.service._launching)

    def test_an_adopted_bottle_takes_room(self) -> None:
        self.limits = HostBudget(memory_gb=2)
        with mock.patch.object(self.service, "allocation", return_value=_running(1)), \
             self.assertRaises(Die):
            self.launch("second")


if __name__ == "__main__":
    unittest.main()
