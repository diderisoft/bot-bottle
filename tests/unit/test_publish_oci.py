"""Publishing to an OCI-only registry (GHCR): the rootfs goes up as an OCI
artifact beside the images, preflight asks the registry API, and promotion
copies images between registries byte for byte. Runs against the in-process
registry in `_fake_oci_registry`."""

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import oci_registry
from bot_bottle import publish_images as pi
from bot_bottle import publish_promote as pp
from bot_bottle.published_artifacts import Artifact, FORMAT_OCI, parse_artifacts_manifest

from ._fake_oci_registry import SECRET, USER, FakeRegistry

# pylint: disable=protected-access
# `_promote_oci` and `_sha256` are exercised directly: promotion's copy is the
# part worth pinning.

_HASH = "9f2c1e4b7a83d10c"


class Defaults(unittest.TestCase):
    def test_images_and_rootfs_go_to_ghcr_by_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual("ghcr.io/diderisoft/bot-bottle-gateway",
                             pi.oci_ref("bot-bottle-gateway"))
            self.assertEqual("ghcr.io/diderisoft/bot-bottle-gateway-rootfs:1.0.0-arm64",
                             pi.ext4_url("bot-bottle-gateway", "1.0.0", "linux/arm64"))
            self.assertEqual("https://github.com/diderisoft/bot-bottle", pi.source_url())

    def test_the_generic_store_keeps_gitea_download_urls(self) -> None:
        env = {pi.EXT4_STORE_ENV: "generic",
               "BOT_BOTTLE_INFRA_ARTIFACT_BASE": "https://gitea.example"}
        with mock.patch.dict("os.environ", env):
            url = pi.ext4_url("img", "1.0.0", "linux/amd64")
        self.assertTrue(url.startswith("https://gitea.example/api/packages/"))

    def test_an_unknown_store_is_refused(self) -> None:
        with mock.patch.dict("os.environ", {pi.EXT4_STORE_ENV: "s3"}):
            with self.assertRaises(pi.PublishError):
                pi.ext4_store()

    def test_gitea_only_steps_do_nothing_on_the_oci_store(self) -> None:
        with mock.patch.dict("os.environ", {pi.EXT4_STORE_ENV: "oci"}), \
                mock.patch("urllib.request.urlopen") as opened:
            pi.delete_published(["img"], ["linux/amd64"], "1.0.0", "tok")
            pi.link_to_repo(["img"], "tok")
        opened.assert_not_called()

    def test_a_manifest_entry_pins_an_oci_rootfs(self) -> None:
        """What a release writes, the loader reads."""
        with mock.patch.dict("os.environ", {}, clear=True):
            built = pi.BuiltImage(name="img", version="1.0.0", protocol=1, artifacts=[
                pi.BuiltArtifact(fmt="ext4", platform="linux/amd64",
                                 digest="sha256:" + "b" * 64, locator="x",
                                 namespace="img-rootfs", inputs_hash=_HASH)])
            entry = pi.artifacts_manifest_entry(built)
        artifact = parse_artifacts_manifest({"img": entry})["img"].artifacts[0]
        self.assertEqual("ghcr.io/diderisoft/img-rootfs:1.0.0-amd64", artifact.url)


class PublishToRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = self.enterContext(FakeRegistry())
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = mock.patch.dict("os.environ", {
            "BOT_BOTTLE_INFRA_ARTIFACT_BASE": f"http://{self.fake.host}",
            "BOT_BOTTLE_INFRA_ARTIFACT_OWNER": "acme",
            pi.TOKEN_ENV: SECRET, pi.USER_ENV: USER, pi.EXT4_STORE_ENV: "oci",
        })
        env.start()
        self.addCleanup(env.stop)

    def _gz(self) -> Path:
        gz = self.tmp / "rootfs.amd64.ext4.gz"
        with gzip.open(gz, "wb") as out:
            out.write(b"rootfs")
        return gz

    def test_rootfs_is_published_as_an_artifact_with_the_file_digest(self) -> None:
        gz = self._gz()
        digest = pi.publish_ext4("img", "1.0.0", "linux/amd64", gz, SECRET)
        self.assertEqual(f"sha256:{pi._sha256(gz)}", digest)
        body, _ = self.fake.manifests[("acme/img-rootfs", "1.0.0-amd64")]
        annotations = json.loads(body)["annotations"]
        self.assertEqual("1.0.0", annotations["org.opencontainers.image.version"])

    def test_preflight_finds_what_is_published(self) -> None:
        self.assertEqual([], pi.already_published(["img"], ["linux/amd64"], "1.0.0", SECRET))
        pi.publish_ext4("img", "1.0.0", "linux/amd64", self._gz(), SECRET)
        self.assertEqual([f"{self.fake.host}/acme/img-rootfs:1.0.0-amd64"],
                         pi.already_published(["img"], ["linux/amd64"], "1.0.0", SECRET))

    def test_a_refused_push_is_a_publish_error(self) -> None:
        with mock.patch.dict("os.environ", {pi.TOKEN_ENV: "wrong"}):
            with self.assertRaises(pi.PublishError):
                pi.publish_ext4("img", "1.0.0", "linux/amd64", self._gz(), "wrong")

    def test_promotion_copies_an_image_from_another_registry(self) -> None:
        with FakeRegistry() as source:
            src = oci_registry.Registry(source.host, user=USER, token=SECRET, scheme="http")
            config = b"{}"
            src.upload_blob("dev/img", f"sha256:{_sha(config)}", config)
            image = json.dumps({"schemaVersion": 2, "mediaType": oci_registry.MANIFEST_OCI,
                                "config": {"digest": f"sha256:{_sha(config)}", "size": 2,
                                           "mediaType": "application/vnd.oci.empty.v1+json"},
                                "layers": []}).encode()
            digest = src.put_manifest("dev/img", "0.0.0-dev-amd64", image,
                                      oci_registry.MANIFEST_OCI)
            artifact = Artifact(image="img", format=FORMAT_OCI, platform="linux/amd64",
                                url=f"{source.host}/dev/img:0.0.0-dev-amd64", digest=digest)
            real = oci_registry.Registry

            def over_http(host: str, user: str = "", token: str = "",
                          scheme: str = "https") -> oci_registry.Registry:
                del scheme
                return real(host, user=user, token=token, scheme="http")

            with mock.patch.object(pi.oci_registry, "Registry", side_effect=over_http):
                promoted = pp._promote_oci(artifact, "1.0.0")
        self.assertEqual(digest, promoted)
        self.assertIn(("acme/img", "1.0.0-amd64"), self.fake.manifests)

    def test_a_failed_promotion_is_a_publish_error(self) -> None:
        artifact = Artifact(image="img", format=FORMAT_OCI, platform="linux/amd64",
                            url="no-registry-host/img:t", digest="sha256:" + "a" * 64)
        with self.assertRaises(pi.PublishError):
            pp._promote_oci(artifact, "1.0.0")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


if __name__ == "__main__":
    unittest.main()
