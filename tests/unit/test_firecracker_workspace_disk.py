"""Unit: Firecracker's per-project workspace disk (#641)."""

from __future__ import annotations

import fcntl
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.backend.firecracker import launch as fc_launch
from bot_bottle.backend.firecracker import util as fc_util
from bot_bottle.backend.firecracker import workspace_disk as wd
from bot_bottle.cli.commands.cleanup_workspaces import cmd_cleanup_workspaces
from bot_bottle.cli.settings import settings_from_env
from bot_bottle.control.hostctl import budget
from bot_bottle.lib.log import Die
from bot_bottle.paths import host_db_path
from bot_bottle.workspace import resync_clear_script
from tests.unit import as_cli, serve_host_controller, use_bottle_root


def _fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
    """mke2fs and truncate as sparse files; resize2fs a no-op."""
    if argv[0] == "mke2fs":
        size = argv[-1]
        Path(argv[-2]).write_bytes(b"")
        with open(argv[-2], "r+b") as f:
            f.truncate(_bytes(size))
    elif argv[0] == "truncate":
        with open(argv[-1], "r+b") as f:
            f.truncate(_bytes(argv[2]))
    return subprocess.CompletedProcess(argv, 0, "", "")


def _failing_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 1, "", "no")


def _only_firecracker(_settings: object, name: str) -> bool:
    return name == "firecracker"


def _bytes(size: str) -> int:
    return int(size[:-1]) * (1024 ** 3 if size.endswith("G") else 1024 ** 2)


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="ws-disk.")  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.addCleanup(use_bottle_root(self.tmp / "root"))
        self.env = self.tmp / "fc"
        patcher = mock.patch.object(fc_util, "env_dir", return_value=self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        run = mock.patch.object(wd.subprocess, "run", side_effect=_fake_run)
        self.fake_run: mock.MagicMock = run.start()
        self.addCleanup(run.stop)
        self.project = self.tmp / "proj"
        self.project.mkdir()

    def acquire(self, slug: str, size: str = "1024M") -> wd.WorkspaceDisk | None:
        return wd.acquire(self.project, "claude", ("dev",), slug=slug, size=size)

    def run_dir(self, slug: str) -> None:
        (self.env / "run" / slug).mkdir(parents=True)


class TestRequestedSize(unittest.TestCase):
    def test_off_on_and_sizes(self) -> None:
        for off in ("", "0", "off", "no"):
            self.assertIsNone(wd.requested_size(off))
        self.assertEqual(wd.DEFAULT_SIZE, wd.requested_size("1"))
        self.assertEqual(wd.DEFAULT_SIZE, wd.requested_size("on"))
        self.assertEqual("32768M", wd.requested_size("32G"))
        self.assertEqual("8192M", wd.requested_size("8192m"))

    def test_nonsense_and_out_of_range_die(self) -> None:
        for bad in ("lots", "16T", "100M", "2048G"):
            with self.subTest(bad=bad), redirect_stderr(io.StringIO()):
                with self.assertRaises(Die):
                    wd.requested_size(bad)


class TestDiskKey(unittest.TestCase):
    def test_project_agent_and_bottle_stack_name_the_disk(self) -> None:
        p = Path("/tmp")
        key = wd.disk_key(p, "claude", ("dev",))
        self.assertEqual(key, wd.disk_key(p, "claude", ("dev",)))
        self.assertNotEqual(key, wd.disk_key(p, "codex", ("dev",)))
        self.assertNotEqual(key, wd.disk_key(p, "claude", ("dev", "github")))
        self.assertNotEqual(key, wd.disk_key(Path("/var"), "claude", ("dev",)))


class TestAcquire(_Root):
    def test_first_use_makes_a_sparse_owner_only_disk(self) -> None:
        disk = self.acquire("a")
        assert disk is not None
        self.addCleanup(disk.release)
        self.assertTrue(disk.fresh)
        self.assertEqual("", disk.last_slug)
        self.assertEqual(0o600, disk.path.stat().st_mode & 0o777)
        self.assertEqual(wd.disk_dir(), disk.path.parent)

    def test_a_later_run_reuses_it_and_knows_who_used_it_last(self) -> None:
        first = self.acquire("a")
        assert first is not None
        first.release()
        second = self.acquire("b")
        assert second is not None
        self.addCleanup(second.release)
        self.assertFalse(second.fresh)
        self.assertEqual("a", second.last_slug)
        self.assertEqual(first.path, second.path)

    def test_it_is_never_attached_twice(self) -> None:
        first = self.acquire("a")
        assert first is not None
        self.addCleanup(first.release)
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(self.acquire("b"))

    def test_a_running_owner_after_a_controller_restart_keeps_it(self) -> None:
        # The lock went with the old controller; the owner's run dir is still there.
        first = self.acquire("a")
        assert first is not None
        assert first._lock is not None  # pylint: disable=protected-access
        first._lock.close()  # pylint: disable=protected-access
        first._lock = None  # pylint: disable=protected-access
        self.run_dir("a")
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(self.acquire("b"))
        self.assertTrue(wd.in_use(first.path))

    def test_a_gone_owner_frees_it(self) -> None:
        first = self.acquire("a")
        assert first is not None
        assert first._lock is not None  # pylint: disable=protected-access
        first._lock.close()  # pylint: disable=protected-access
        first._lock = None  # pylint: disable=protected-access
        second = self.acquire("b")
        assert second is not None
        second.release()
        second.release()  # idempotent
        self.assertFalse(wd.in_use(second.path))

    def test_it_grows_but_never_shrinks(self) -> None:
        disk = self.acquire("a", "1024M")
        assert disk is not None
        disk.release()
        disk = self.acquire("b", "2048M")
        assert disk is not None
        disk.release()
        self.assertEqual(_bytes("2048M"), disk.path.stat().st_size)
        disk = self.acquire("c", "1024M")
        assert disk is not None
        disk.release()
        self.assertEqual(_bytes("2048M"), disk.path.stat().st_size)

    def test_a_failed_mke2fs_dies_and_leaves_nothing(self) -> None:
        self.fake_run.side_effect = _failing_run
        with redirect_stderr(io.StringIO()), self.assertRaises(Die):
            self.acquire("a")
        self.assertEqual([], list(wd.disk_dir().glob("*.ext4")))

    def test_a_failed_grow_warns_and_carries_on(self) -> None:
        disk = self.acquire("a", "1024M")
        assert disk is not None
        disk.release()
        self.fake_run.side_effect = _failing_run
        with redirect_stderr(io.StringIO()) as err:
            again = self.acquire("b", "2048M")
        assert again is not None
        again.release()
        self.assertIn("couldn't grow", err.getvalue())


class TestListingAndRemove(_Root):
    def test_lists_and_removes_only_what_is_free(self) -> None:
        busy = self.acquire("a")
        assert busy is not None
        self.addCleanup(busy.release)
        other = wd.acquire(self.tmp / "gone", "claude", ("dev",), slug="b", size="1024M")
        assert other is not None
        other.release()
        [first, second] = sorted(wd.listing(), key=lambda d: d.project)
        by_project = {d.project: d for d in (first, second)}
        gone = by_project[str((self.tmp / "gone").resolve())]
        self.assertTrue(gone.orphaned)
        self.assertFalse(gone.in_use)
        self.assertFalse(wd.remove(busy.path))
        self.assertTrue(wd.remove(gone.path))
        self.assertFalse(gone.path.exists())

    def test_remove_skips_a_disk_with_a_running_owner(self) -> None:
        disk = self.acquire("a")
        assert disk is not None
        assert disk._lock is not None  # pylint: disable=protected-access
        disk._lock.close()  # pylint: disable=protected-access
        disk._lock = None  # pylint: disable=protected-access
        self.run_dir("a")
        self.assertFalse(wd.remove(disk.path))

    def test_cleanup_workspaces_removes_orphans_or_with_all_every_free_one(self) -> None:
        only_fc = mock.patch("bot_bottle.cli.commands.cleanup_workspaces.has_backend",
                             side_effect=_only_firecracker)
        only_fc.start()
        self.addCleanup(only_fc.stop)
        kept = self.acquire("a")
        assert kept is not None
        kept.release()
        gone = wd.acquire(self.tmp / "gone", "claude", ("dev",), slug="b", size="1024M")
        assert gone is not None
        gone.release()
        # The CLI reads and removes through the host controller.
        serve_host_controller(self)
        as_cli(self)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(0, cmd_cleanup_workspaces(["--dry-run"]))
        self.assertIn("project gone", err.getvalue())
        self.assertTrue(gone.path.exists())
        with redirect_stderr(io.StringIO()):
            cmd_cleanup_workspaces([])
        self.assertFalse(gone.path.exists())
        self.assertTrue(kept.path.exists())
        with redirect_stderr(io.StringIO()):
            cmd_cleanup_workspaces(["--all"])
        self.assertFalse(kept.path.exists())
        with redirect_stderr(io.StringIO()) as err:
            cmd_cleanup_workspaces([])
        self.assertIn("no workspace disks", err.getvalue())
        self.assertEqual([], list(wd.disk_dir().iterdir()))  # no lock or sidecar left

    def test_its_record_is_a_host_db_row(self) -> None:
        disk = self.acquire("a")
        assert disk is not None
        self.addCleanup(disk.release)
        self.assertEqual([disk.path.name, disk.path.with_suffix(".lock").name],
                         sorted(p.name for p in wd.disk_dir().iterdir()))
        meta = wd.WorkspaceDiskStore(host_db_path()).get(disk.path.stem)
        self.assertEqual((str(self.project.resolve()), "claude", ["dev"], "a"),
                         (meta["project"], meta["agent"], meta["bottles"], meta["owner"]))

    def test_remove_takes_only_a_workspace_disk(self) -> None:
        with self.assertRaises(ValueError):
            wd.remove(wd.disk_dir() / ".." / "secrets")

    def test_listing_with_no_dir_is_empty(self) -> None:
        self.assertEqual([], wd.listing())


class TestWorkspaceMode(unittest.TestCase):
    def disk(self, *, fresh: bool = False, last: str = "") -> wd.WorkspaceDisk:
        return wd.WorkspaceDisk(path=Path("/x.ext4"), fresh=fresh, last_slug=last)

    def test_modes(self) -> None:
        mode = wd.workspace_mode
        self.assertEqual("copy", mode(None, slug="a", home_kept=False, used_disk_last=False))
        self.assertEqual("keep", mode(None, slug="a", home_kept=True, used_disk_last=False))
        # Last run's workspace was the disk: the home holds an empty mount point.
        self.assertEqual("copy", mode(None, slug="a", home_kept=True, used_disk_last=True))
        self.assertEqual("copy", mode(self.disk(fresh=True), slug="a", home_kept=True,
                                      used_disk_last=False))
        self.assertEqual("keep", mode(self.disk(last="a"), slug="a", home_kept=True,
                                      used_disk_last=True))
        # Another bottled agent used it since: back to the project as it is now.
        self.assertEqual("resync", mode(self.disk(last="b"), slug="a", home_kept=True,
                                        used_disk_last=True))
        self.assertEqual("resync", mode(self.disk(last="a"), slug="a", home_kept=False,
                                        used_disk_last=False))

    def test_guest_path_and_boot_arg(self) -> None:
        self.assertTrue(wd.guest_path_ok("/home/node/workspace"))
        self.assertFalse(wd.guest_path_ok("/home/node/my work"))
        self.assertFalse(wd.guest_path_ok("relative"))
        self.assertEqual("bb_workspace=/home/node/workspace",
                         wd.boot_arg("/home/node/workspace"))


class TestResyncClearScript(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="ws-resync.")  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)

    def sh(self, script: str) -> int:
        return subprocess.run(["sh", "-c", script], capture_output=True, check=False).returncode

    def test_a_repo_keeps_only_what_it_ignores(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True)
        (self.ws / ".gitignore").write_text("node_modules/\nbuild/\n")
        (self.ws / "app.py").write_text("x\n")
        (self.ws / "notes.txt").write_text("wip\n")
        (self.ws / "node_modules").mkdir()
        (self.ws / "node_modules" / "dep.js").write_text("y\n")
        (self.ws / "build").mkdir()
        (self.ws / "build" / "out.o").write_text("z\n")
        subprocess.run(["git", "-C", str(self.ws), "add", "app.py", ".gitignore"], check=True)
        self.assertEqual(0, self.sh(resync_clear_script(str(self.ws), is_git=True)))
        self.assertFalse((self.ws / "app.py").exists())
        self.assertFalse((self.ws / "notes.txt").exists())
        self.assertFalse((self.ws / ".gitignore").exists())
        self.assertFalse((self.ws / ".git").exists())
        self.assertTrue((self.ws / "node_modules" / "dep.js").exists())
        self.assertTrue((self.ws / "build" / "out.o").exists())

    def test_a_broken_repo_fails_so_the_copy_starts_over(self) -> None:
        (self.ws / ".git").mkdir()
        (self.ws / "app.py").write_text("x\n")
        self.assertNotEqual(0, self.sh(resync_clear_script(str(self.ws), is_git=True)))
        self.assertTrue((self.ws / "app.py").exists())

    def test_outside_a_repo_it_keeps_the_skipped_dirs_at_any_depth(self) -> None:
        (self.ws / "src").mkdir()
        (self.ws / "src" / "a.py").write_text("x\n")
        (self.ws / "pkg" / "node_modules").mkdir(parents=True)
        (self.ws / "pkg" / "node_modules" / "dep.js").write_text("y\n")
        (self.ws / ".venv").mkdir()
        (self.ws / ".venv" / "bin").write_text("z\n")
        self.assertEqual(0, self.sh(resync_clear_script(str(self.ws), is_git=False)))
        self.assertFalse((self.ws / "src" / "a.py").exists())
        self.assertTrue((self.ws / "pkg" / "node_modules" / "dep.js").exists())
        self.assertTrue((self.ws / ".venv" / "bin").exists())


def _plan(project: Path, setting: str, *, guest_path: str = "/home/node/workspace",
          enabled: bool = True) -> Any:
    return SimpleNamespace(
        slug="demo-1",
        settings=SimpleNamespace(operation=SimpleNamespace(fc_workspace_disk=setting)),
        spec=SimpleNamespace(agent_name="claude", bottle_names=("dev",)),
        workspace_plan=SimpleNamespace(enabled=enabled, copy_contents=True,
                                       host_path=project, guest_path=guest_path),
    )


class TestLaunchWiring(_Root):
    def marker(self) -> Path:
        from bot_bottle.bottle_state import bottle_state_dir  # pylint: disable=import-outside-toplevel
        return bottle_state_dir("demo-1") / wd.STATE_MARKER

    def test_off_copies_into_the_home_as_before(self) -> None:
        disk, mode = fc_launch._workspace_disk(  # pylint: disable=protected-access
            _plan(self.project, ""), home_kept=True)
        self.assertIsNone(disk)
        self.assertEqual("keep", mode)

    def test_on_attaches_the_disk_and_marks_the_bottle(self) -> None:
        disk, mode = fc_launch._workspace_disk(  # pylint: disable=protected-access
            _plan(self.project, "1"), home_kept=False)
        assert disk is not None
        self.addCleanup(disk.release)
        self.assertEqual("copy", mode)
        self.assertTrue(self.marker().exists())

    def test_turning_it_off_after_a_disk_run_copies_afresh(self) -> None:
        disk, _ = fc_launch._workspace_disk(  # pylint: disable=protected-access
            _plan(self.project, "1"), home_kept=False)
        assert disk is not None
        disk.release()
        disk, mode = fc_launch._workspace_disk(  # pylint: disable=protected-access
            _plan(self.project, ""), home_kept=True)
        self.assertIsNone(disk)
        self.assertEqual("copy", mode)
        self.assertFalse(self.marker().exists())

    def test_a_path_the_cmdline_cant_carry_copies_instead(self) -> None:
        with redirect_stderr(io.StringIO()) as err:
            disk, mode = fc_launch._workspace_disk(  # pylint: disable=protected-access
                _plan(self.project, "1", guest_path="/home/my work"),
                home_kept=False)
        self.assertIsNone(disk)
        self.assertEqual("copy", mode)
        self.assertIn("can't take a workspace disk", err.getvalue())

    def test_no_workspace_no_disk(self) -> None:
        disk, _ = fc_launch._workspace_disk(  # pylint: disable=protected-access
            _plan(self.project, "1", enabled=False), home_kept=False)
        self.assertIsNone(disk)


class TestWiring(unittest.TestCase):
    def test_the_env_var_sets_the_operation_setting(self) -> None:
        settings = settings_from_env({"BOT_BOTTLE_FC_WORKSPACE_DISK": "32G"})
        self.assertEqual("32G", settings.operation.fc_workspace_disk)
        # Omitted at its default, so an older host controller still parses it.
        self.assertNotIn("fc_workspace_disk", settings_from_env({}).to_json()["operation"])

    def test_the_budget_counts_the_disks_as_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "workspace-disks" / "k.ext4"
            disk.parent.mkdir()
            disk.write_bytes(b"x" * 8192)
            totals = budget.walk(((budget.STATE, Path(tmp)),))
        self.assertGreater(totals[budget.WORKSPACES], 0)
        self.assertEqual(0, totals[budget.VM_DISKS])

    def test_the_guest_init_mounts_the_disk_at_the_named_path(self) -> None:
        init = fc_util._GUEST_INIT  # pylint: disable=protected-access
        self.assertIn("bb_workspace=", init)
        self.assertIn('mount /dev/vdc "$WS"', init)
        # After the home is mounted, since the workspace sits in it.
        self.assertLess(init.index("mount /dev/vdb /home/node"), init.index("/dev/vdc"))

    def test_flock_conflicts_within_one_process(self) -> None:
        # What `acquire` relies on: two opens of the lock file conflict.
        with tempfile.NamedTemporaryFile() as f:
            with open(f.name, "a", encoding="utf-8") as a, open(f.name, "a", encoding="utf-8") as b:
                fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(OSError):
                    fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
