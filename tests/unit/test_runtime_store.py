"""Unit: what Docker and Apple `container` hold for bot-bottle, for the
storage budget (`backend/runtime_store.py`)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from bot_bottle.backend import runtime_store as rs

# The shape of `docker system df -v --format '{{json .}}'` (Docker 27).
DOCKER_DF: dict[str, object] = {
    "Images": [
        {"ID": "sha256:a", "Repository": "bot-bottle-gateway", "Tag": "latest",
         "Size": "360MB", "SharedSize": "358.2MB", "UniqueSize": "1.394MB"},
        # The same image under another tag counts once.
        {"ID": "sha256:a", "Repository": "bot-bottle-gateway", "Tag": "7518",
         "Size": "360MB", "SharedSize": "358.2MB", "UniqueSize": "1.394MB"},
        {"ID": "sha256:b", "Repository": "ghcr.io/diderisoft/bot-bottle-claude",
         "Tag": "0.1.6", "Size": "1.2GB", "SharedSize": "300MB", "UniqueSize": "900MB"},
        {"ID": "sha256:c", "Repository": "python", "Tag": "3.13",
         "Size": "1GB", "SharedSize": "0B", "UniqueSize": "1GB"},
    ],
    "Containers": [
        {"Names": "bot-bottle-weather", "Image": "bot-bottle-claude:pin", "Size": "144kB"},
        {"Names": "bot-bottle-gw", "Image": "bot-bottle-gateway:latest",
         "Size": "2MB (virtual 360MB)"},
        {"Names": "bb-ci-7524-orch-gateway", "Image": "bot-bottle-gateway:latest",
         "Size": "5GB"},
        {"Names": "postgres", "Image": "postgres:16", "Size": "3GB"},
    ],
    "Volumes": [
        {"Name": "bot-bottle-home-weather", "Size": "1.5GB"},
        {"Name": "bb-ci-7524-ca", "Size": "9.412kB"},
        {"Name": "pgdata", "Size": "10GB"},
    ],
    "BuildCache": [{"ID": "x", "Size": "4GB"}],
}


def _fails(message: str):
    def run(_: Sequence[str]) -> str:
        raise RuntimeError(message)
    return run


class TestParseSize(unittest.TestCase):
    def test_docker_sizes(self) -> None:
        self.assertEqual(144_000, rs.parse_size("144kB"))
        self.assertEqual(1_394_000, rs.parse_size("1.394MB"))
        self.assertEqual(2_000_000, rs.parse_size("2MB (virtual 360MB)"))
        self.assertEqual(0, rs.parse_size("N/A"))
        self.assertEqual(0, rs.parse_size("xGB"))


class TestDocker(unittest.TestCase):
    def test_counts_bot_bottles_images_once_and_this_environments_disks(self) -> None:
        got = rs.docker_usage_from(DOCKER_DF, "bot-bottle-")
        # Unique bytes of each bot-bottle image, plus the largest shared share.
        self.assertEqual(1_394_000 + 900_000_000 + 358_200_000, got.images)
        self.assertEqual(144_000 + 2_000_000 + 1_500_000_000, got.vm_disks)
        self.assertEqual((), got.unknown)

    def test_another_environments_disks_are_its_own(self) -> None:
        got = rs.docker_usage_from(DOCKER_DF, "bb-ci-7524-")
        self.assertEqual(5_000_000_000 + 9_412, got.vm_disks)

    def test_asks_docker_system_df(self) -> None:
        argv: list[Sequence[str]] = []

        def run(a: Sequence[str]) -> str:
            argv.append(a)
            return json.dumps(DOCKER_DF)

        self.assertGreater(rs.docker_usage("bot-bottle-", run).images, 0)
        self.assertEqual(["docker", "system", "df", "-v", "--format", "{{json .}}"],
                         list(argv[0]))

    def test_unreachable_or_unreadable_is_unknown_not_zero(self) -> None:
        down = rs.docker_usage("p", _fails("Cannot connect to the Docker daemon"))
        self.assertEqual(("docker: Cannot connect to the Docker daemon",), down.unknown)
        self.assertEqual(1, len(rs.docker_usage("p", lambda _: "not json").unknown))
        self.assertEqual(("docker: unexpected `system df` output",),
                         rs.docker_usage("p", lambda _: "[]").unknown)


# The shape of `container image list --format json` (Apple container 0.x).
CONTAINER_IMAGES = [
    {"configuration": {"name": "bot-bottle-claude:pin-1"},
     "variants": [{"digest": "sha256:arm", "size": 700}, {"digest": "sha256:amd", "size": 650}]},
    # A second tag of the same variants counts once.
    {"configuration": {"name": "ghcr.io/diderisoft/bot-bottle-claude:0.1.6"},
     "variants": [{"digest": "sha256:arm", "size": 700}]},
    {"configuration": {"name": "docker.io/library/python:3.13"},
     "variants": [{"digest": "sha256:py", "size": 9000}]},
    {"configuration": {}}, "junk",
]


class TestAppleContainer(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.store = Path(self.enterContext(tmp))

    def _disk(self, kind: str, name: str, size: int) -> None:
        d = self.store / kind / name
        d.mkdir(parents=True)
        (d / "rootfs.ext4").write_bytes(b"x" * size)

    def test_images_by_variant_and_this_environments_containers_and_volumes(self) -> None:
        self._disk("containers", "bot-bottle-weather", 8192)
        self._disk("volumes", "bot-bottle-home", 4096)
        self._disk("containers", "bb-dev-other", 4096)
        self._disk("containers", "someone-else", 4096)
        got = rs.container_usage("bot-bottle-", lambda _: json.dumps(CONTAINER_IMAGES),
                                 self.store)
        self.assertEqual(700 + 650, got.images)
        self.assertGreaterEqual(got.vm_disks, 8192 + 4096)
        self.assertLess(got.vm_disks, 3 * 8192 + 4096)
        self.assertEqual((), got.unknown)

    def test_no_store_counts_images_and_says_so(self) -> None:
        got = rs.container_usage("bot-bottle-", lambda _: json.dumps(CONTAINER_IMAGES),
                                 self.store / "missing")
        self.assertEqual(1350, got.images)
        self.assertEqual(0, got.vm_disks)
        self.assertIn("no store", got.unknown[0])

    def test_unreachable_is_unknown(self) -> None:
        got = rs.container_usage("p", _fails("XPC connection error"), self.store)
        self.assertEqual(("container: XPC connection error",), got.unknown)
        self.assertEqual(0, rs.container_usage("p", lambda _: "{}", self.store).images)


class TestRun(unittest.TestCase):
    def test_output_or_a_short_reason(self) -> None:
        self.assertEqual("hi\n", rs._run(["sh", "-c", "echo hi"]))  # pylint: disable=protected-access
        with self.assertRaisesRegex(RuntimeError, "nope"):
            rs._run(["sh", "-c", "echo nope >&2; exit 3"])  # pylint: disable=protected-access
        with self.assertRaisesRegex(RuntimeError, "exit 4"):
            rs._run(["sh", "-c", "exit 4"])  # pylint: disable=protected-access
        with self.assertRaises(RuntimeError):
            rs._run(["/nonexistent/binary"])  # pylint: disable=protected-access

    def test_a_hung_runtime_times_out(self) -> None:
        with mock.patch.object(subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["docker"], 60)):
            with self.assertRaisesRegex(RuntimeError, "no answer in 60s"):
                rs._run(["docker"])  # pylint: disable=protected-access


class TestRuntimeUsage(unittest.TestCase):
    def test_asks_only_the_runtimes_on_the_path(self) -> None:
        with mock.patch.object(rs, "docker_usage", return_value=rs.StoreUsage(1, 2)) as d, \
                mock.patch.object(rs, "container_usage",
                                  return_value=rs.StoreUsage(3, 4, ("c: x",))) as c:
            self.assertEqual(rs.StoreUsage(), rs.runtime_usage(lambda _: None))
            got = rs.runtime_usage(lambda name: f"/usr/bin/{name}")
            only_docker = rs.runtime_usage(lambda name: "/x" if name == "docker" else None)
        self.assertEqual(rs.StoreUsage(4, 6, ("c: x",)), got)
        self.assertEqual(rs.StoreUsage(1, 2), only_docker)
        self.assertEqual(2, d.call_count)
        self.assertEqual(1, c.call_count)

    def test_unreadable_files_in_the_store_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "containers" / "bot-bottle-x"
            d.mkdir(parents=True)
            (d / "a").write_bytes(b"x" * 4096)
            os.symlink("/nonexistent", d / "dangling")
            with mock.patch.object(os, "lstat", side_effect=OSError("gone")):
                self.assertEqual(0, rs.container_disks_from(Path(tmp), "bot-bottle-"))
            self.assertGreaterEqual(rs.container_disks_from(Path(tmp), "bot-bottle-"), 4096)


if __name__ == "__main__":
    unittest.main()
