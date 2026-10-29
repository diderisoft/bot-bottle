"""Unit: `bot-bottle cleanup images` (PRD 0088): what stays, what goes, and how each
backend lists its cached images."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bot_bottle.backend import oci_cache_entries as oci
from bot_bottle.backend.cache_entries import CacheEntry, human, to_remove
from bot_bottle.backend.firecracker import cache_entries as fc
from bot_bottle.backend.firecracker import image_builder
from bot_bottle.cli.commands import cleanup_images as prune

_DAY = 86400.0
_LIB = "docker.io/library/"
_NOW = 1_800_000_000.0


def _entry(what: str, *, age_days: float, keep: str | None = None,
           build: bool = True) -> CacheEntry:
    return CacheEntry(what, 1, _NOW - age_days * _DAY, keep, lambda: True, build)


class Rule(unittest.TestCase):
    def test_what_goes(self) -> None:
        entries = [
            _entry("current pin", age_days=90, keep="a current pin", build=False),
            _entry("old pin", age_days=1, build=False),          # a release: at once
            _entry("old build", age_days=20),                    # a build past the cutoff
            _entry("recent build", age_days=3),                  # a build still in use
            _entry("in use", age_days=90, keep="in use by a container"),
        ]
        going = [e.what for e in to_remove(entries, older_than_days=14, now=_NOW)]
        self.assertEqual(["old pin", "old build"], going)

    def test_older_than_moves_the_cutoff(self) -> None:
        going = to_remove([_entry("b", age_days=3)], older_than_days=2, now=_NOW)
        self.assertEqual(["b"], [e.what for e in going])

    def test_sizes_read_short(self) -> None:
        self.assertEqual(["812 MB", "3.4 GB"], [human(812_000_000), human(3_400_000_000)])


class OciImages(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(
            oci, "_pins", return_value=({"bot-bottle-claude:pin-aaaaaaaaaaaaaaaa"},
                                        {"sha256:" + "a" * 64})))

    def entries(self, *images: oci.LocalImage) -> dict[str, CacheEntry]:
        return {e.what.removeprefix("image "): e for e in oci.entries_for(images, lambda _r: True)}

    def test_pins_in_use_and_the_newest_build_stay(self) -> None:
        found = self.entries(
            oci.LocalImage(f"{_LIB}bot-bottle-claude:pin-{'a' * 16}", 1, 1, False),
            oci.LocalImage(f"{_LIB}bot-bottle-claude:pin-{'b' * 16}", 1, 1, False),
            oci.LocalImage("reg:5000/didericis/bot-bottle-claude@sha256:" + "a" * 64, 1, 1, False),
            oci.LocalImage("reg:5000/didericis/bot-bottle-claude@sha256:" + "b" * 64, 1, 1, False),
            oci.LocalImage("bot-bottle-gateway:src-old", 1, 10, False),
            oci.LocalImage("bot-bottle-gateway:src-new", 1, 20, False),
            oci.LocalImage("bot-bottle-gateway:src-running", 1, 5, True),
            oci.LocalImage("bot-bottle-claude:overlay-k1", 1, 1, False),
        )
        keep = {ref: e.keep for ref, e in found.items()}
        self.assertEqual("a current pin", keep[f"{_LIB}bot-bottle-claude:pin-{'a' * 16}"])
        self.assertIsNone(keep[f"{_LIB}bot-bottle-claude:pin-{'b' * 16}"])
        registry = "reg:5000/didericis/bot-bottle-claude@sha256:"
        self.assertEqual("a current pin", keep[registry + "a" * 64])
        self.assertIsNone(keep[registry + "b" * 64])
        self.assertEqual("the newest bot-bottle-gateway build", keep["bot-bottle-gateway:src-new"])
        self.assertIsNone(keep["bot-bottle-gateway:src-old"])
        self.assertEqual("in use by a container", keep["bot-bottle-gateway:src-running"])
        self.assertTrue(found["bot-bottle-claude:overlay-k1"].build)
        self.assertFalse(found[f"{_LIB}bot-bottle-claude:pin-{'b' * 16}"].build)

    def test_latest_frozen_bottles_and_other_images_are_never_listed(self) -> None:
        found = self.entries(
            oci.LocalImage("bot-bottle-claude:latest", 1, 1, False),
            oci.LocalImage("bot-bottle-committed-x:latest", 1, 1, False),
            oci.LocalImage("bot-bottle-committed-x:pin-1", 1, 1, False),
            oci.LocalImage("docker.io/library/python:3.13", 1, 1, False),
        )
        self.assertEqual({}, found)


class FirecrackerCache(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "fc"
        self.rootfs = self.cache / "rootfs"
        self.rootfs.mkdir(parents=True)
        self.artifacts = Path(tmp.name) / "artifacts"
        self.enterContext(mock.patch.object(fc.util, "cache_dir", return_value=self.cache))
        self.enterContext(mock.patch.object(fc, "host_artifact_cache_dir",
                                            return_value=self.artifacts))
        self.pinned_ext4 = self.rootfs / "agent-ext4-1111.ext4"
        self.enterContext(mock.patch.object(fc, "_pinned_ext4_paths",
                                            return_value={self.pinned_ext4}))
        self.enterContext(mock.patch.object(fc, "_pinned_digests",
                                            return_value={"sha256:" + "c" * 64}))

    def tree(self, name: str, *, used: float) -> Path:
        path = self.rootfs / name
        path.mkdir()
        (path / "file").write_bytes(b"x" * 4096)
        marker = path / ".bb-ready"
        marker.write_text("ok\n")
        os.utime(marker, (used, used))
        return path

    def test_lists_every_kind_and_keeps_pins_and_the_newest_role_tree(self) -> None:
        for name in ("agent-ext4-1111", "agent-ext4-2222"):
            (self.rootfs / f"{name}.ext4").write_bytes(b"x" * 4096)
            (self.rootfs / f"{name}.ready").write_text("ok\n")
        self.tree("dead-orchestrator-0001", used=100)
        self.tree("beef-orchestrator-0001", used=200)
        self.tree("aaaa-gateway-0002", used=50)
        self.tree("agent-overlay-9", used=10)
        self.tree("cafe-bot-bottle-gateway-amd64", used=10)
        (self.rootfs / ".staging-agent-x").mkdir()
        (self.rootfs / ".build.lock").write_text("")
        for digest in ("c" * 64, "d" * 64):
            (self.artifacts / "bot-bottle-pi" / f"sha256-{digest}").mkdir(parents=True)
        infra = SimpleNamespace(artifact_cache="")
        entries = {e.what: e for e in fc.list_entries(infra)}  # type: ignore[arg-type]
        self.assertEqual("a current pin", entries["agent base agent-ext4-1111.ext4"].keep)
        self.assertIsNone(entries["agent base agent-ext4-2222.ext4"].keep)
        self.assertFalse(entries["agent base agent-ext4-2222.ext4"].build)
        self.assertEqual("the current orchestrator tree",
                         entries["orchestrator tree beef-orchestrator-0001"].keep)
        self.assertIsNone(entries["orchestrator tree dead-orchestrator-0001"].keep)
        self.assertEqual("the current gateway tree", entries["gateway tree aaaa-gateway-0002"].keep)
        self.assertIn("agent build agent-overlay-9", entries)
        self.assertIn("interrupted build .staging-agent-x", entries)
        self.assertIsNotNone(entries[f"download bot-bottle-pi/sha256-{'c' * 12}"].keep)
        self.assertIsNone(entries[f"download bot-bottle-pi/sha256-{'d' * 12}"].keep)
        self.assertFalse(any(".build.lock" in w for w in entries))

    def test_removing_an_agent_base_takes_its_markers(self) -> None:
        image = self.rootfs / "agent-ext4-2222.ext4"
        image.write_bytes(b"x")
        image.with_suffix(".ready").write_text("ok\n")
        image.with_suffix(".smoked").write_text("ok\n")
        infra = SimpleNamespace(artifact_cache="")
        [entry] = [e for e in fc.list_entries(infra)  # type: ignore[arg-type]
                   if "2222" in e.what]  # pylint: disable=unbalanced-tuple-unpacking
        self.assertTrue(entry.remove())
        self.assertEqual([], sorted(p.name for p in self.rootfs.iterdir()))

    def test_a_cache_hit_refreshes_the_marker(self) -> None:
        tree = self.tree("agent-overlay-hit", used=100)
        with mock.patch.object(image_builder, "_rootfs_dir", return_value=tree):
            self.assertEqual(tree, image_builder._ready_dir("agent-overlay-hit"))  # pylint: disable=protected-access
        self.assertGreater((tree / ".bb-ready").stat().st_mtime, time.time() - 60)


class PruneCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.removed: list[str] = []

        def remover(name: str):  # type: ignore[no-untyped-def]
            def remove() -> bool:
                self.removed.append(name)
                return name != "stuck"
            return remove
        old = time.time() - 30 * _DAY
        self.backend = SimpleNamespace(name="macos-container", cache_entries=lambda: [
            CacheEntry("image old-pin", 300_000_000, old, None, remover("old-pin"), False),
            CacheEntry("image current", 300_000_000, old, "a current pin", remover("current"),
                       False),
            CacheEntry("image recent-build", 1, time.time(), None, remover("recent"), True),
        ])
        self.enterContext(mock.patch.object(prune, "_backends", return_value=[self.backend]))

    def run_cmd(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = prune.cmd_cleanup_images(argv)
        return code, out.getvalue()

    def test_dry_run_lists_and_removes_nothing(self) -> None:
        code, out = self.run_cmd(["--dry-run"])
        self.assertEqual(0, code)
        self.assertIn("would remove image old-pin (300 MB)", out)
        self.assertIn("kept 2 of 3", out)
        self.assertIn("would free 300 MB", out)
        self.assertEqual([], self.removed)

    def test_prune_removes_what_may_go(self) -> None:
        code, out = self.run_cmd([])
        self.assertEqual((0, ["old-pin"]), (code, self.removed))
        self.assertIn("freed 300 MB", out)

    def test_a_failed_removal_is_reported(self) -> None:
        self.backend.cache_entries = lambda: [
            CacheEntry("image stuck", 1, 0, None, lambda: False, False)]
        code, out = self.run_cmd([])
        self.assertEqual(1, code)
        self.assertIn("couldn't remove image stuck", out)

    def test_the_build_store_is_left_while_the_vm_uses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "build-store.ext4"
            store.write_bytes(b"x" * 4096)
            with mock.patch.object(fc, "build_store", return_value=(store, True)):
                freed, line = fc.prune_build_store(dry_run=False) or (None, "")
            self.assertEqual(0, freed)
            self.assertTrue(store.exists())
            self.assertIn("in use by the orchestrator VM", line)
            with mock.patch.object(fc, "build_store", return_value=(store, False)):
                freed, line = fc.prune_build_store(dry_run=False) or (None, "")
            self.assertFalse(store.exists())
            self.assertIn("removed the buildah store", line)

    def test_the_command_asks_each_backend_for_its_build_store(self) -> None:
        self.backend.prune_build_store = mock.Mock(return_value=(5_000_000, "removed it"))
        _code, out = self.run_cmd(["--build-store", "--dry-run"])
        self.backend.prune_build_store.assert_called_once_with(dry_run=True)
        self.assertIn("macos-container: removed it", out)

class Helpers(unittest.TestCase):
    def test_disk_size_mtime_and_remove(self) -> None:
        from bot_bottle.backend.cache_entries import disk_size, mtime, remove_paths
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            (tree / "sub").mkdir(parents=True)
            (tree / "sub" / "f").write_bytes(b"x" * 8192)
            self.assertGreater(disk_size(tree), 0)
            self.assertEqual(0, disk_size(Path(tmp) / "missing"))
            self.assertEqual(7.0, mtime(Path(tmp) / "missing", 7.0))
            single = Path(tmp) / "one"
            single.write_text("x")
            self.assertTrue(remove_paths(tree, single, Path(tmp) / "never")())
            self.assertFalse(tree.exists() or single.exists())
        self.assertEqual("1.2 TB", human(1_200_000_000_000))

    def test_backends_without_caches_list_nothing(self) -> None:
        from bot_bottle.backend.base import BottleBackend
        self.assertEqual([], BottleBackend.cache_entries(mock.Mock()))
        self.assertIsNone(BottleBackend.prune_build_store(mock.Mock(), dry_run=True))


class Pins(unittest.TestCase):
    def test_oci_pins_name_each_harness_tag_and_digest(self) -> None:
        pin = SimpleNamespace(digest="sha256:" + "e" * 64)
        with mock.patch.object(oci, "resolve_agent_pin", return_value=pin):
            tags, digests = oci._pins()  # pylint: disable=protected-access
        self.assertIn(f"bot-bottle-pi:pin-{'e' * 16}", tags)
        self.assertEqual({pin.digest}, digests)

    def test_a_ref_splits_into_repo_and_tag(self) -> None:
        split = oci._split  # pylint: disable=protected-access
        self.assertEqual(("bot-bottle-pi", "pin-1"), split("docker.io/library/bot-bottle-pi:pin-1"))
        self.assertEqual(("bot-bottle-pi", "@sha256:ab"),
                         split("reg:5000/x/bot-bottle-pi@sha256:ab"))
        self.assertEqual(("bot-bottle-pi", ""), split("reg:5000/x/bot-bottle-pi"))

    def test_firecracker_pins_cover_agent_bases_and_infra_roles(self) -> None:
        pin = SimpleNamespace(digest="sha256:" + "f" * 64)
        role = SimpleNamespace(digest="sha256:" + "9" * 64)
        infra = SimpleNamespace()
        ext4 = Path("/c/agent-ext4-x.ext4")
        with mock.patch.object(fc, "resolve_agent_pin", return_value=pin), \
                mock.patch.object(fc.infra_rootfs, "role_artifact", return_value=role), \
                mock.patch.object(fc.image_builder, "_image_ext4_path", return_value=ext4):
            digests = fc._pinned_digests(infra)  # type: ignore[arg-type]  # pylint: disable=protected-access
            paths = fc._pinned_ext4_paths(infra)  # type: ignore[arg-type]  # pylint: disable=protected-access
        self.assertEqual(({pin.digest, role.digest}, {ext4}), (digests, paths))
        with mock.patch.object(fc, "resolve_agent_pin", side_effect=RuntimeError("no manifest")), \
                mock.patch.object(fc.infra_rootfs, "role_artifact", side_effect=RuntimeError()):
            self.assertEqual(set(), fc._pinned_digests(infra))  # type: ignore[arg-type]  # pylint: disable=protected-access


def _completed(out: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], 0, out, "")


class RuntimeListers(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(oci, "_pins", return_value=(set(), set())))

    def test_macos_reads_images_sizes_dates_and_containers(self) -> None:
        from bot_bottle.backend.macos_container import cache_entries as mac
        listings = {
            "list": [{"configuration": {"image": {
                "reference": "docker.io/library/bot-bottle-gateway:src-a"}}}],
            "image": [
                {"configuration": {"name": "bot-bottle-gateway:src-a",
                                   "creationDate": "2026-10-01T00:00:00Z"},
                 "variants": [{"size": 100}, {"size": 20}]},
                {"configuration": {"name": "bot-bottle-gateway:src-b",
                                   "creationDate": "not a date"}, "variants": []},
                {"configuration": {}},
            ],
        }

        def run(argv: list[str], **_k: object) -> "subprocess.CompletedProcess[str]":
            return _completed(json.dumps(listings[argv[1]]))
        with mock.patch("bot_bottle.backend.host_resources.subprocess.run", side_effect=run):
            found = {e.what: e for e in mac.list_entries()}
        src_a = found["image bot-bottle-gateway:src-a"]
        self.assertEqual((120, "in use by a container"), (src_a.size, src_a.keep))
        self.assertEqual(0.0, found["image bot-bottle-gateway:src-b"].last_used)

    def test_docker_reads_images_sizes_dates_and_containers(self) -> None:
        from bot_bottle.backend.docker import cache_entries as dock
        rows = [
            {"Repository": "bot-bottle-claude", "Tag": "pin-1", "ID": "abc", "Size": "1.5GB",
             "CreatedAt": "2026-10-24 21:51:29 -0400 EDT"},
            {"Repository": "bot-bottle-claude", "Tag": "overlay-2", "ID": "def", "Size": "12kB",
             "CreatedAt": "garbage"},
            {"Repository": "bot-bottle-claude", "Tag": "<none>", "ID": "x", "Size": "1MB"},
        ]

        def run(argv: list[str], **_k: object) -> "subprocess.CompletedProcess[str]":
            if argv[1] == "ps":
                return _completed("abc\n")
            return _completed("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        with mock.patch("bot_bottle.backend.host_resources.subprocess.run", side_effect=run):
            found = {e.what: e for e in dock.list_entries()}
        pin = found["image bot-bottle-claude:pin-1"]
        self.assertEqual((1_500_000_000, "in use by a container"), (pin.size, pin.keep))
        self.assertGreater(pin.last_used, 0)
        self.assertEqual(12_000, found["image bot-bottle-claude:overlay-2"].size)
        self.assertEqual(2, len(found))
        self.assertEqual(0, dock._size("lots"))  # pylint: disable=protected-access


class BackendWiring(unittest.TestCase):
    def test_each_backend_hands_over_its_listing(self) -> None:
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        from bot_bottle.backend.firecracker.backend import FirecrackerBottleBackend
        from bot_bottle.backend.macos_container.backend import MacosContainerBottleBackend
        for cls, module in ((DockerBottleBackend, "bot_bottle.backend.docker.cache_entries"),
                            (MacosContainerBottleBackend,
                             "bot_bottle.backend.macos_container.cache_entries"),
                            (FirecrackerBottleBackend,
                             "bot_bottle.backend.firecracker.cache_entries")):
            backend = cls.__new__(cls)
            backend.settings = SimpleNamespace(infra=SimpleNamespace())  # type: ignore[assignment]
            with mock.patch(f"{module}.list_entries", return_value=["x"]):
                self.assertEqual(["x"], backend.cache_entries())
        fcb = FirecrackerBottleBackend.__new__(FirecrackerBottleBackend)
        with mock.patch("bot_bottle.backend.firecracker.cache_entries.prune_build_store",
                        return_value=(1, "ok")):
            self.assertEqual((1, "ok"), fcb.prune_build_store(dry_run=True))


class AgentBaseHits(unittest.TestCase):
    def test_a_cached_agent_ext4_refreshes_its_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "agent-ext4-x.ext4"
            image.write_bytes(b"x")
            ready = image.with_suffix(".ready")
            ready.write_text("ok\n")
            os.utime(ready, (100, 100))
            with mock.patch.object(image_builder, "_image_ext4_path", return_value=image):
                self.assertEqual(image, image_builder.cached_agent_image_ext4(
                    mock.Mock(), mock.Mock()))
            self.assertGreater(ready.stat().st_mtime, time.time() - 60)
            ready.unlink()
            with mock.patch.object(image_builder, "_image_ext4_path", return_value=image):
                self.assertIsNone(image_builder.cached_agent_image_ext4(mock.Mock(), mock.Mock()))


class CommandEdges(unittest.TestCase):
    def test_a_backend_that_cannot_list_is_reported_and_skipped(self) -> None:
        broken = SimpleNamespace(name="docker",
                                 cache_entries=mock.Mock(side_effect=OSError("down")))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(prune, "_backends", return_value=[broken]), \
                redirect_stdout(out), mock.patch("sys.stderr", err):
            self.assertEqual(0, prune.cmd_cleanup_images([]))
        self.assertIn("couldn't list cached images", err.getvalue())

    def test_backends_are_the_usable_ones(self) -> None:
        def usable(_settings: object, name: str) -> bool:
            return name == "b"

        def backend(_settings: object, name: str) -> str:
            return name
        with mock.patch.object(prune, "known_backend_names", return_value=["a", "b"]), \
                mock.patch.object(prune, "has_backend", side_effect=usable), \
                mock.patch.object(prune, "get_bottle_backend", side_effect=backend), \
                mock.patch.object(prune, "settings_from_env"):
            self.assertEqual(["b"], prune._backends())  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
