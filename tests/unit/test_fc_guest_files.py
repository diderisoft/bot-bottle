"""Unit: the Firecracker guest kernel and dropbear, fetched by digest."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle import publish_fc_guest
from bot_bottle.backend.firecracker import guest_files
from bot_bottle.contract.hostctl import InfraSettings
from bot_bottle.oci_registry import OciRegistryError, Registry


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Registry(Registry):
    """Serves blobs by digest; records what was asked for."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        super().__init__("registry.test")
        self.blobs = blobs
        self.asked: list[str] = []

    def download_blob(self, repo: str, digest: str, dest: Path) -> None:
        self.asked.append(f"{repo}@{digest}")
        if digest not in self.blobs:
            raise OciRegistryError("HTTP 404")
        dest.write_bytes(self.blobs[digest])


class Ensure(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        cache = patch("bot_bottle.backend.firecracker.util.cache_dir", return_value=self.cache)
        cache.start()
        self.addCleanup(cache.stop)
        self.kernel, self.dropbear = b"kernel", b"dropbear"
        digests = patch.dict(guest_files.DIGESTS, {"x86_64": {
            guest_files.KERNEL: _digest(self.kernel),
            guest_files.DROPBEAR: _digest(self.dropbear)}})
        digests.start()
        self.addCleanup(digests.stop)

    def test_missing_files_are_fetched_into_the_cache_by_digest(self) -> None:
        registry = _Registry({_digest(self.kernel): self.kernel,
                              _digest(self.dropbear): self.dropbear})
        said: list[str] = []
        fetched = guest_files.ensure(InfraSettings(), say=said.append,
                                     registry=registry, machine="x86_64")
        self.assertEqual(["vmlinux", "dropbear"], fetched)
        self.assertEqual(self.kernel, (self.cache / "vmlinux").read_bytes())
        self.assertEqual(self.dropbear, (self.cache / "dropbear").read_bytes())
        self.assertEqual(0o555, (self.cache / "dropbear").stat().st_mode & 0o777)
        self.assertEqual(f"{guest_files.REPO}@{_digest(self.kernel)}", registry.asked[0])
        self.assertEqual(2, len(said))
        # Nothing left to fetch.
        self.assertEqual([], guest_files.missing(InfraSettings()))

    def test_files_the_operator_set_are_never_fetched(self) -> None:
        registry = _Registry({})
        infra = InfraSettings(fc_kernel="/mine/vmlinux", fc_dropbear="/mine/dropbear")
        self.assertEqual([], guest_files.ensure(infra, registry=registry,
                                                machine="x86_64"))
        self.assertEqual([], registry.asked)

    def test_an_architecture_without_files_says_what_to_set(self) -> None:
        with self.assertRaisesRegex(guest_files.GuestFilesError, "BOT_BOTTLE_FC_KERNEL"):
            guest_files.ensure(InfraSettings(), machine="riscv64")

    def test_a_failed_download_is_an_error(self) -> None:
        with self.assertRaisesRegex(guest_files.GuestFilesError, "couldn't download"):
            guest_files.ensure(InfraSettings(), registry=_Registry({}),
                               machine="x86_64")


class Publish(unittest.TestCase):
    def test_publish_pushes_each_file_and_a_manifest_naming_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel, dropbear = Path(tmp) / "vmlinux", Path(tmp) / "dropbear"
            kernel.write_bytes(b"k")
            dropbear.write_bytes(b"d")
            registry = MagicMock()
            pinned = guest_files.publish(registry, "x86_64",
                                         {"vmlinux": kernel, "dropbear": dropbear})
        self.assertEqual({"vmlinux": _digest(b"k"), "dropbear": _digest(b"d")}, pinned)
        repo, tag, body, _media = registry.put_manifest.call_args.args
        self.assertEqual((guest_files.REPO, "x86_64"), (repo, tag))
        titles = [layer["annotations"]["org.opencontainers.image.title"]
                  for layer in json.loads(body)["layers"]]
        self.assertEqual(["dropbear", "vmlinux"], titles)
        self.assertEqual(3, registry.upload_blob.call_count)

    def test_main_prints_the_digests_to_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kernel, dropbear = Path(tmp) / "vmlinux", Path(tmp) / "dropbear"
            kernel.write_bytes(b"k")
            dropbear.write_bytes(b"d")
            with patch.object(publish_fc_guest, "Registry"), \
                    patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(0, publish_fc_guest.main(["x86_64", str(kernel), str(dropbear)]))
            self.assertIn(f"x86_64 vmlinux {_digest(b'k')}", out.getvalue())
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(2, publish_fc_guest.main([]))


if __name__ == "__main__":
    unittest.main()
