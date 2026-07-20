"""Unit: the prebuilt infra-rootfs artifact pull (PRD 0069 Stage 2).

The launch-host half — version hashing and download/verify/decompress — is
what keeps a docker-free host from booting a stale or corrupted rootfs, so the
checksum + fail-closed paths are locked here. Network is mocked; no Docker.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from bot_bottle.backend.firecracker import infra_artifact as ia
from bot_bottle.log import Die


def _gz(data: bytes) -> bytes:
    return gzip.compress(data)


class _FakeNet:
    """Map artifact URLs to bytes (or an HTTPError) for urlopen."""

    def __init__(self, responses: "dict[str, bytes | Exception]") -> None:
        self._responses = responses
        self.calls: list[str] = []

    def urlopen(self, req: urllib.request.Request, *a: object, **k: object) -> io.BytesIO:
        url = req.full_url
        self.calls.append(url)
        val = self._responses.get(url)
        if isinstance(val, Exception):
            raise val
        if val is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        return io.BytesIO(val)


class _CacheMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {"BOT_BOTTLE_FC_CACHE": self._tmp.name,
             "BOT_BOTTLE_INFRA_ARTIFACT_TOKEN": "",
             # Pin the candidate-dir override off: the coverage CI job exports a
             # candidate dir for the integration suite, and an ambient value
             # would send these registry-pull tests down the local-bundle path.
             # Cases that exercise the candidate path set it explicitly.
             "BOT_BOTTLE_INFRA_ARTIFACT_DIR": ""},
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _serve(self, version: str, gz_bytes: bytes, sha_text: str | None = None):
        if sha_text is None:
            sha_text = f"{hashlib.sha256(gz_bytes).hexdigest()}  rootfs.ext4.gz\n"
        net = _FakeNet({
            ia.artifact_url(version, "rootfs.ext4.gz"): gz_bytes,
            ia.artifact_url(version, "rootfs.ext4.gz.sha256"): sha_text.encode(),
        })
        return mock.patch.object(ia.urllib.request, "urlopen", net.urlopen), net


class TestVersion(unittest.TestCase):
    def test_deterministic_16_hex(self) -> None:
        v = ia.infra_artifact_version("#!/bin/sh\ntrue\n")
        self.assertEqual(v, ia.infra_artifact_version("#!/bin/sh\ntrue\n"))
        self.assertEqual(16, len(v))
        int(v, 16)  # hex

    def test_init_change_bumps_version(self) -> None:
        self.assertNotEqual(
            ia.infra_artifact_version("a"), ia.infra_artifact_version("b"))


class TestVersionInputs(unittest.TestCase):
    """The hash must cover *every* file baked into the rootfs, not just `*.py`
    (`COPY bot_bottle` is wholesale) — else a non-Python change (e.g. the egress
    entrypoint shell script) leaves the version unchanged and a launch host
    boots a rootfs whose code differs from its checkout."""

    def _fake_repo(self, root: Path) -> None:
        pkg = root / "bot_bottle"
        pkg.mkdir()
        (pkg / "app.py").write_text("print('hi')\n")
        (pkg / "egress_entrypoint.sh").write_text("#!/bin/sh\nexec mitmdump\n")
        (pkg / "netpool.defaults.env").write_text("FOO=1\n")
        for name in ("Dockerfile.orchestrator", "Dockerfile.gateway", "Dockerfile.infra"):
            (root / name).write_text(f"FROM scratch  # {name}\n")
        (root / "pyproject.toml").write_text("[project]\nname = 'bot-bottle'\n")

    def test_pyproject_toml_change_bumps_version(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_repo(root)
            before = ia.infra_artifact_version("init", repo_root=root)
            (root / "pyproject.toml").write_text(
                "[project]\nname = 'bot-bottle'\ndependencies = ['httpx']\n")
            after = ia.infra_artifact_version("init", repo_root=root)
        self.assertNotEqual(before, after)

    def test_dropbear_change_bumps_version(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_repo(root)
            dropbear = root / "dropbear"
            dropbear.write_bytes(b"dropbear-v1")
            with mock.patch.dict(os.environ, {
                "BOT_BOTTLE_FC_DROPBEAR": str(dropbear),
            }):
                before = ia.infra_artifact_version("init", repo_root=root)
                dropbear.write_bytes(b"dropbear-v2")
                after = ia.infra_artifact_version("init", repo_root=root)
        self.assertNotEqual(before, after)

    def test_non_python_file_change_bumps_version(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_repo(root)
            before = ia.infra_artifact_version("init", repo_root=root)
            (root / "bot_bottle" / "egress_entrypoint.sh").write_text(
                "#!/bin/sh\nexec mitmdump --different\n")
            after = ia.infra_artifact_version("init", repo_root=root)
        self.assertNotEqual(before, after)

    def test_pyc_and_pycache_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_repo(root)
            before = ia.infra_artifact_version("init", repo_root=root)
            cache = root / "bot_bottle" / "__pycache__"
            cache.mkdir()
            (cache / "app.cpython-312.pyc").write_bytes(b"\x00bytecode")
            (root / "bot_bottle" / "app.pyc").write_bytes(b"\x00bytecode")
            after = ia.infra_artifact_version("init", repo_root=root)
        self.assertEqual(before, after)


class TestEnsureArtifact(_CacheMixin):
    def test_uses_verified_ci_candidate_without_network(self) -> None:
        version = "deadbeef00000000"
        gz = _gz(b"candidate ext4")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "version.txt").write_text(version + "\n")
            (root / "rootfs.ext4.gz").write_bytes(gz)
            digest = hashlib.sha256(gz).hexdigest()
            (root / "rootfs.ext4.gz.sha256").write_text(
                f"{digest}  rootfs.ext4.gz\n")
            with mock.patch.dict(os.environ, {
                "BOT_BOTTLE_INFRA_ARTIFACT_DIR": str(root),
            }), mock.patch.object(ia.urllib.request, "urlopen") as net:
                path = ia.ensure_artifact_gz(version)
            self.assertEqual(root / "rootfs.ext4.gz", path)
            net.assert_not_called()

    def test_rejects_candidate_for_another_version(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "version.txt").write_text("wrong\n")
            with mock.patch.dict(os.environ, {
                "BOT_BOTTLE_INFRA_ARTIFACT_DIR": str(root),
            }):
                with self.assertRaises(Die) as ctx:
                    ia.ensure_artifact_gz("expected")
            self.assertIn("version mismatch", str(ctx.exception.message))

    def test_rejects_incomplete_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "version.txt").write_text("v1\n")
            with mock.patch.dict(os.environ, {
                "BOT_BOTTLE_INFRA_ARTIFACT_DIR": str(root),
            }):
                with self.assertRaises(Die) as ctx:
                    ia.ensure_artifact_gz("v1")
            self.assertIn("incomplete", str(ctx.exception.message))

    def test_rejects_candidate_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "version.txt").write_text("v1\n")
            (root / "rootfs.ext4.gz").write_bytes(b"bad")
            (root / "rootfs.ext4.gz.sha256").write_text("0" * 64 + "  rootfs.ext4.gz\n")
            with mock.patch.dict(os.environ, {
                "BOT_BOTTLE_INFRA_ARTIFACT_DIR": str(root),
            }):
                with self.assertRaises(Die) as ctx:
                    ia.ensure_artifact_gz("v1")
            self.assertIn("checksum mismatch", str(ctx.exception.message))

    def test_downloads_verifies_and_caches(self) -> None:
        version = "deadbeef00000000"
        gz = _gz(b"fake ext4 bytes")
        patcher, net = self._serve(version, gz)
        with patcher:
            path = ia.ensure_artifact_gz(version)
            self.assertTrue(path.is_file())
            self.assertEqual(gz, path.read_bytes())
            first_calls = len(net.calls)
            # Second call is a cache hit — no further network.
            ia.ensure_artifact_gz(version)
            self.assertEqual(first_calls, len(net.calls))

    def test_checksum_mismatch_fails_closed(self) -> None:
        version = "beefbeefbeefbeef"
        gz = _gz(b"payload")
        patcher, _ = self._serve(version, gz, sha_text="0" * 64 + "  rootfs.ext4.gz\n")
        with patcher:
            with self.assertRaises(Die) as ctx:
                ia.ensure_artifact_gz(version)
        self.assertIn("checksum mismatch", str(ctx.exception.message))
        # nothing left cached to accidentally boot
        self.assertFalse((ia._cache_root(version) / "rootfs.ext4.gz").exists())

    def test_missing_artifact_points_at_publish(self) -> None:
        version = "0000000000000000"
        net = _FakeNet({})  # everything 404s
        with mock.patch.object(ia.urllib.request, "urlopen", net.urlopen):
            with self.assertRaises(Die) as ctx:
                ia.ensure_artifact_gz(version)
        self.assertIn("publish_infra", str(ctx.exception.message))

    def test_materialize_gunzips_to_dest(self) -> None:
        version = "1234123412341234"
        raw = b"the real rootfs contents" * 100
        patcher, _ = self._serve(version, _gz(raw))
        with patcher, tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "rootfs.ext4"
            ia.materialize_ext4(version, dest)
            self.assertEqual(raw, dest.read_bytes())


class TestConfig(unittest.TestCase):
    def test_base_and_owner_overridable(self) -> None:
        with mock.patch.dict(os.environ, {
            "BOT_BOTTLE_INFRA_ARTIFACT_BASE": "https://mirror.example/",
            "BOT_BOTTLE_INFRA_ARTIFACT_OWNER": "acme",
        }):
            url = ia.artifact_url("v1", "rootfs.ext4.gz")
        self.assertEqual(
            "https://mirror.example/api/packages/acme/generic/"
            "bot-bottle-firecracker-infra/v1/rootfs.ext4.gz", url)

    def test_local_build_flag(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_INFRA_BUILD": "local"}):
            self.assertTrue(ia.local_build_requested())
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_INFRA_BUILD": ""}):
            self.assertFalse(ia.local_build_requested())


if __name__ == "__main__":
    unittest.main()
