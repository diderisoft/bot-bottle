"""The stdlib OCI registry client, against an in-process registry that behaves
like GHCR: a Bearer token realm, anonymous pulls, pushes only with credentials,
blob downloads redirected to a CDN."""

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import image_registry, oci_registry
from bot_bottle.published_artifacts import Artifact, FORMAT_EXT4

from ._fake_oci_registry import SECRET, USER, FakeRegistry


def _gz(tmp: Path, payload: bytes = b"ext4-bytes") -> Path:
    path = tmp / "rootfs.amd64.ext4.gz"
    with gzip.open(path, "wb") as out:
        out.write(payload)
    return path


class ParseRef(unittest.TestCase):
    def test_host_repo_and_tag(self) -> None:
        ref = oci_registry.parse_ref("ghcr.io/acme/app-rootfs:1.0.0-amd64")
        self.assertEqual(("ghcr.io", "acme/app-rootfs", "1.0.0-amd64"),
                         (ref.host, ref.repo, ref.tag))
        self.assertEqual("ghcr.io/acme/app-rootfs", ref.name)

    def test_a_port_is_not_a_tag(self) -> None:
        ref = oci_registry.parse_ref("localhost:5000/acme/app")
        self.assertEqual(("localhost:5000", "acme/app", ""), (ref.host, ref.repo, ref.tag))

    def test_urls_and_hostless_refs_are_refused(self) -> None:
        for bad in ("https://ghcr.io/acme/app", "acme/app:1", "ghcr.io", "ghcr.io/a@sha256:x"):
            with self.subTest(bad), self.assertRaises(oci_registry.OciRegistryError):
                oci_registry.parse_ref(bad)


class RootfsArtifact(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = self.enterContext(FakeRegistry())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _pusher(self) -> oci_registry.Registry:
        return oci_registry.Registry(self.fake.host, user=USER, token=SECRET, scheme="http")

    def test_pushed_rootfs_downloads_anonymously_by_digest(self) -> None:
        gz = _gz(self.tmp)
        digest = oci_registry.push_rootfs(self._pusher(), "acme/app-rootfs", "1.0.0-amd64", gz,
                                          annotations={"org.opencontainers.image.version": "1.0.0"})
        self.assertEqual(oci_registry.sha256_file(gz), digest)

        body, media = self.fake.manifests[("acme/app-rootfs", "1.0.0-amd64")]
        manifest = json.loads(body)
        self.assertEqual(oci_registry.MANIFEST_OCI, media)
        self.assertEqual(oci_registry.ROOTFS_ARTIFACT_TYPE, manifest["artifactType"])
        self.assertEqual(digest, manifest["layers"][0]["digest"])

        anonymous = oci_registry.Registry(self.fake.host, scheme="http")
        dest = self.tmp / "out.gz"
        anonymous.download_blob("acme/app-rootfs", digest, dest)
        self.assertEqual(gz.read_bytes(), dest.read_bytes())

    def test_credentials_do_not_follow_the_cdn_redirect(self) -> None:
        gz = _gz(self.tmp)
        digest = oci_registry.push_rootfs(self._pusher(), "acme/r", "t", gz, annotations={})
        self._pusher().download_blob("acme/r", digest, self.tmp / "out.gz")
        self.assertFalse(self.fake.cdn_auth_seen)

    def test_pushing_without_credentials_is_refused(self) -> None:
        anonymous = oci_registry.Registry(self.fake.host, scheme="http")
        with self.assertRaises(oci_registry.OciRegistryError):
            oci_registry.push_rootfs(anonymous, "acme/r", "t", _gz(self.tmp), annotations={})

    def test_a_blob_already_there_is_not_uploaded_again(self) -> None:
        gz = _gz(self.tmp)
        oci_registry.push_rootfs(self._pusher(), "acme/r", "a", gz, annotations={})
        before = len(self.fake.uploaded)
        oci_registry.push_rootfs(self._pusher(), "acme/r", "b", gz, annotations={})
        self.assertEqual(before, len(self.fake.uploaded))

    def test_manifest_exists(self) -> None:
        registry = self._pusher()
        self.assertFalse(registry.manifest_exists("acme/r", "t"))
        oci_registry.push_rootfs(registry, "acme/r", "t", _gz(self.tmp), annotations={})
        self.assertTrue(registry.manifest_exists("acme/r", "t"))

    def test_a_blob_that_hashes_wrong_is_refused(self) -> None:
        self.fake.blobs["sha256:" + "0" * 64] = b"not those bytes"
        anonymous = oci_registry.Registry(self.fake.host, scheme="http")
        with self.assertRaises(oci_registry.OciRegistryError):
            anonymous.download_blob("acme/r", "sha256:" + "0" * 64, self.tmp / "x")
        self.assertFalse((self.tmp / "x").exists())

    def test_a_missing_blob_is_an_error(self) -> None:
        anonymous = oci_registry.Registry(self.fake.host, scheme="http")
        with self.assertRaises(oci_registry.OciRegistryError):
            anonymous.download_blob("acme/r", "sha256:" + "1" * 64, self.tmp / "x")

    def test_ensure_ext4_fetches_an_oci_rootfs(self) -> None:
        gz = _gz(self.tmp, b"agent rootfs")
        digest = oci_registry.push_rootfs(self._pusher(), "acme/agent-rootfs", "1.0.0-amd64",
                                          gz, annotations={})
        artifact = Artifact(image="agent", format=FORMAT_EXT4, platform="linux/amd64",
                            url=f"{self.fake.host}/acme/agent-rootfs:1.0.0-amd64",
                            digest=digest, version="1.0.0")
        real = oci_registry.Registry

        def over_http(host: str, token: str = "") -> oci_registry.Registry:
            return real(host, token=token, scheme="http")

        with mock.patch.object(image_registry.oci_registry, "Registry", side_effect=over_http):
            cached = image_registry.ensure_ext4(artifact, cache_root=self.tmp / "cache")
        self.assertEqual(gz.read_bytes(), cached.read_bytes())

    def test_ensure_ext4_reports_a_failed_oci_fetch(self) -> None:
        artifact = Artifact(image="agent", format=FORMAT_EXT4, platform="linux/amd64",
                            url="not a ref", digest="sha256:" + "2" * 64, version="1.0.0")
        with self.assertRaises(image_registry.RegistryError):
            image_registry.ensure_ext4(artifact, cache_root=self.tmp / "cache")


class CopyManifest(unittest.TestCase):
    """Promotion copies an image between registries byte for byte, so the
    tested digest is the shipped one."""

    def test_an_index_and_its_children_copy_with_their_digests(self) -> None:
        with FakeRegistry() as src_fake, FakeRegistry() as dst_fake, \
                tempfile.TemporaryDirectory() as scratch:
            src = oci_registry.Registry(src_fake.host, user=USER, token=SECRET, scheme="http")
            dst = oci_registry.Registry(dst_fake.host, user=USER, token=SECRET, scheme="http")
            layer = b"layer-bytes"
            config = b'{"architecture":"amd64"}'
            for blob in (layer, config):
                src.upload_blob("o/img", oci_registry.sha256_file(_write(scratch, blob)),
                                _write(scratch, blob))
            image = json.dumps({
                "schemaVersion": 2, "mediaType": oci_registry.MANIFEST_OCI,
                "config": {"digest": _sha(config), "size": len(config),
                           "mediaType": "application/vnd.oci.image.config.v1+json"},
                "layers": [{"digest": _sha(layer), "size": len(layer),
                            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}],
            }).encode()
            image_digest = src.put_manifest("o/img", "dev", image, oci_registry.MANIFEST_OCI)
            index = json.dumps({"schemaVersion": 2, "mediaType": oci_registry.INDEX_OCI,
                                "manifests": [{"digest": image_digest, "size": len(image),
                                               "mediaType": oci_registry.MANIFEST_OCI}]}).encode()
            index_digest = src.put_manifest("o/img", "dev-index", index, oci_registry.INDEX_OCI)

            copied = oci_registry.copy_manifest(src, "o/img", index_digest, dst=dst,
                                                dst_repo="acme/img", tag="1.0.0-amd64",
                                                scratch=Path(scratch))
            self.assertEqual(index_digest, copied)
            self.assertEqual(index, dst_fake.manifests[("acme/img", "1.0.0-amd64")][0])
            self.assertEqual(image, dst_fake.manifests[("acme/img", image_digest)][0])
            self.assertEqual({_sha(layer), _sha(config)}, set(dst_fake.blobs))


def _write(scratch: str, body: bytes) -> Path:
    path = Path(scratch) / _sha(body).replace(":", "-")
    path.write_bytes(body)
    return path


def _sha(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


if __name__ == "__main__":
    unittest.main()
