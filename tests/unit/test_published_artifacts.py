"""Unit: the published-artifacts manifest, hashing, and registry verification.

Locks what the publish/pull scheme rests on: a digest is verified while a tag is
only checked, and a moved tag is caught; malformed manifests are reported rather
than crashed on; and the flat `{type, platform, url, digest}` asset shape round
-trips between publisher and consumer.

No network and no container runtime: HTTP is mocked at `urlopen`.
"""

# Reference-parsing helpers are private but carry real failure modes (a ref with
# no registry host has nothing to ask about a tag), so they are exercised
# directly rather than only through the public surface.

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle import published_artifacts as pa
from bot_bottle import image_inputs as ii
from bot_bottle import image_registry as ir
from bot_bottle import publish_images as pi

_A = "sha256:" + "a" * 64
_B = "sha256:" + "b" * 64
_C = "sha256:" + "c" * 64

_OCI_URL = "gitea.dideric.is/didericis/bot-bottle-gateway:0.14.2-arm64"
_EXT4_URL = (
    "https://gitea.dideric.is/api/packages/didericis/generic/"
    "bot-bottle-gateway-rootfs/0.14.2/rootfs.arm64.ext4.gz"
)


def _entry(**over: object) -> dict[str, Any]:
    data = {
        "version": "0.14.2",
        "inputs_hash": "9f2c1e4b7a83d10c",
        "artifacts": {
            "oci_linux-arm64": {
                "type": "oci", "platform": "linux-arm64",
                "url": _OCI_URL, "digest": _A,
            },
            "ext4_linux-arm64": {
                "type": "ext4", "platform": "linux-arm64",
                "url": _EXT4_URL, "digest": _B,
            },
        },
    }
    data.update(over)  # type: ignore[arg-type]
    return {"bot-bottle-gateway": data}


class ParseManifest(unittest.TestCase):
    def test_parses_both_formats(self) -> None:
        entries = pa.parse_artifacts_manifest(_entry())
        entry = entries["bot-bottle-gateway"]
        self.assertEqual(entry.version, "0.14.2")
        self.assertEqual({a.format for a in entry.artifacts}, {"oci", "ext4"})

    def test_platform_is_slash_spelled_in_code(self) -> None:
        """On-disk `linux-arm64` becomes in-code `linux/arm64` at the boundary."""
        entries = pa.parse_artifacts_manifest(_entry())
        self.assertEqual(
            {a.platform for a in entries["bot-bottle-gateway"].artifacts},
            {"linux/arm64"},
        )

    def test_key_must_equal_type_platform(self) -> None:
        """The key/field redundancy is a load-time integrity check."""
        bad = _entry()
        asset = bad["bot-bottle-gateway"]["artifacts"].pop("oci_linux-arm64")
        bad["bot-bottle-gateway"]["artifacts"]["oci_linux-amd64"] = asset  # key lies
        problems: list[str] = []
        pa.parse_artifacts_manifest(bad, problems=problems)
        self.assertTrue(any("key must equal" in p for p in problems))

    def test_rejects_short_digest(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["oci_linux-arm64"]["digest"] = "sha256:abc"
        with self.assertRaises(pa.ArtifactsManifestError):
            pa.parse_artifacts_manifest(bad)

    def test_rejects_latest_version(self) -> None:
        with self.assertRaises(pa.ArtifactsManifestError):
            pa.parse_artifacts_manifest(_entry(version="latest"))

    def test_rejects_bad_inputs_hash(self) -> None:
        with self.assertRaises(pa.ArtifactsManifestError):
            pa.parse_artifacts_manifest(_entry(inputs_hash="nope"))

    def test_rejects_unsupported_platform(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["oci_linux-arm64"]["platform"] = "linux-riscv64"
        with self.assertRaises(pa.ArtifactsManifestError):
            pa.parse_artifacts_manifest(bad)

    def test_rejects_slash_spelled_platform_on_disk(self) -> None:
        """On disk the spelling is dash; a slash is a malformed manifest."""
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["oci_linux-arm64"]["platform"] = "linux/arm64"
        with self.assertRaises(pa.ArtifactsManifestError):
            pa.parse_artifacts_manifest(bad)

    def test_collects_every_problem_when_asked(self) -> None:
        """The CI checker wants the full list, not just the first fault."""
        bad = _entry(version="latest", inputs_hash="nope")
        problems: list[str] = []
        pa.parse_artifacts_manifest(bad, problems=problems)
        self.assertGreaterEqual(len(problems), 2)


class ValidationBranches(unittest.TestCase):
    """Every malformed shape is reported rather than crashing on it. The CI
    checker collects the full list, so each fault must be survivable — one bad
    entry must not hide the next."""

    def _problems(self, data: object) -> list[str]:
        found: list[str] = []
        pa.parse_artifacts_manifest(data, problems=found)
        return found

    def test_top_level_must_be_an_object(self) -> None:
        self.assertTrue(self._problems(["not", "an", "object"]))

    def test_entry_must_be_an_object(self) -> None:
        self.assertTrue(self._problems({"img": "nope"}))

    def test_missing_version_is_reported(self) -> None:
        bad = _entry()
        del bad["bot-bottle-gateway"]["version"]
        self.assertTrue(any("version" in p for p in self._problems(bad)))

    def test_artifacts_must_be_a_non_empty_object(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"] = {}
        self.assertTrue(any("artifacts" in p for p in self._problems(bad)))

    def test_unknown_type_is_reported(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["squashfs_linux-arm64"] = {
            "type": "squashfs", "platform": "linux-arm64", "url": "u", "digest": _A,
        }
        self.assertTrue(any("squashfs" in p for p in self._problems(bad)))

    def test_asset_must_be_an_object(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["oci_linux-arm64"] = "nope"
        self.assertTrue(self._problems(bad))

    def test_missing_url_is_reported(self) -> None:
        bad = _entry()
        del bad["bot-bottle-gateway"]["artifacts"]["oci_linux-arm64"]["url"]
        self.assertTrue(any("url" in p for p in self._problems(bad)))

    def test_missing_digest_is_reported(self) -> None:
        bad = _entry()
        del bad["bot-bottle-gateway"]["artifacts"]["ext4_linux-arm64"]["digest"]
        self.assertTrue(any("digest" in p for p in self._problems(bad)))

    def test_entry_with_no_valid_artifact_is_dropped(self) -> None:
        """An entry whose artifacts were all rejected is not half-loaded — a
        launch host must never resolve a partially-parsed entry."""
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"] = {"oci_linux-arm64": {
            "type": "oci", "platform": "linux-arm64", "url": "u",
            "digest": "sha256:short",
        }}
        found: list[str] = []
        entries = pa.parse_artifacts_manifest(bad, problems=found)
        self.assertEqual(entries, {})
        self.assertTrue(found)


class Resolution(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = pa.parse_artifacts_manifest(_entry())

    def test_preference_order_is_honoured(self) -> None:
        got = pa.resolve(self.entries, "bot-bottle-gateway",
                         prefer=("ext4", "oci"), plat="linux/arm64")
        self.assertEqual(got.format, "ext4")

    def test_falls_back_to_next_format(self) -> None:
        """A collected ext4 must degrade to slow (convert from OCI), not
        broken — this fallback is what makes narrow ext4 retention safe."""
        only_oci = _entry()
        del only_oci["bot-bottle-gateway"]["artifacts"]["ext4_linux-arm64"]
        entries = pa.parse_artifacts_manifest(only_oci)
        got = pa.resolve(entries, "bot-bottle-gateway",
                         prefer=("ext4", "oci"), plat="linux/arm64")
        self.assertEqual(got.format, "oci")

    def test_missing_platform_is_an_error(self) -> None:
        with self.assertRaises(pa.ArtifactsManifestError):
            pa.resolve(self.entries, "bot-bottle-gateway",
                       prefer=("oci",), plat="linux/amd64")

    def test_unknown_image_names_what_is_available(self) -> None:
        with self.assertRaises(pa.ArtifactsManifestError) as ctx:
            pa.resolve(self.entries, "nope", prefer=("oci",), plat="linux/arm64")
        self.assertIn("bot-bottle-gateway", str(ctx.exception))


class Consistency(unittest.TestCase):
    def test_shared_digest_across_formats_is_a_problem(self) -> None:
        """Two formats are never the same bytes; a shared digest means a
        publish bug copied one across, which would hand a host the wrong
        artifact."""
        dupe = _entry()
        dupe["bot-bottle-gateway"]["artifacts"]["ext4_linux-arm64"]["digest"] = _A
        entries = pa.parse_artifacts_manifest(dupe)
        self.assertTrue(pa.check_consistency(entries))

    def test_clean_manifest_has_no_problems(self) -> None:
        self.assertEqual(pa.check_consistency(pa.parse_artifacts_manifest(_entry())), [])


class InputsHash(unittest.TestCase):
    def test_is_architecture_independent_by_design(self) -> None:
        """One hash per checkout, not per machine.

        Every base image is pinned to a multi-arch *index* digest, so one set of
        inputs describes both architectures and `--platform` specialises the
        build. Architecture is disambiguated structurally instead — by the
        per-asset platform and the per-arch tags and filenames the url carries —
        which is what closes the hole in the older firecracker version hash,
        where architecture appeared in neither the hash nor the artifact path."""
        entries = pa.parse_artifacts_manifest(_entry())
        artifact = pa.resolve(entries, "bot-bottle-gateway",
                              prefer=("oci",), plat="linux/arm64")
        self.assertIn("arm64", artifact.url)
        self.assertEqual(artifact.platform, "linux/arm64")

    def test_distinguishes_image(self) -> None:
        self.assertNotEqual(
            ii.inputs_hash("bot-bottle-gateway"),
            ii.inputs_hash("bot-bottle-claude"),
        )

    def test_is_deterministic(self) -> None:
        self.assertEqual(
            ii.inputs_hash("bot-bottle-codex"),
            ii.inputs_hash("bot-bottle-codex"),
        )

    def test_unknown_image_is_rejected(self) -> None:
        with self.assertRaises(ii.UnknownImageError):
            ii.inputs_hash("bot-bottle-nope")

    def test_every_known_image_has_a_dockerfile(self) -> None:
        self.assertEqual(set(ii.BUILD_INPUTS), set(ii.DOCKERFILES))


class _Resp:
    def __init__(self, digest: str) -> None:
        self.headers = {"Docker-Content-Digest": digest}

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class TagVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.artifact = pa.resolve(
            pa.parse_artifacts_manifest(_entry()), "bot-bottle-gateway",
            prefer=("oci",), plat="linux/arm64",
        )

    def test_matching_tag_passes(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_Resp(_A)):
            ir.verify_tag(self.artifact)

    def test_moved_tag_is_detected(self) -> None:
        """Pinning by digest gives correct bytes but hides a re-pointed tag,
        because the tag is not consulted during a digest pull. This is the
        check that turns 'I'm safe' into 'I'd know'."""
        with mock.patch("urllib.request.urlopen", return_value=_Resp(_C)):
            with self.assertRaises(ir.DigestMismatch) as ctx:
                ir.verify_tag(self.artifact)
        self.assertIn(_A, str(ctx.exception))
        self.assertIn(_C, str(ctx.exception))

    def test_missing_digest_header_fails_closed(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_Resp("")):
            with self.assertRaises(ir.RegistryError):
                ir.verify_tag(self.artifact)

    def test_unpublished_tag_is_named(self) -> None:
        err = urllib.error.HTTPError("u", 404, "nf", None, None)  # type: ignore[arg-type]
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ir.RegistryError) as ctx:
                ir.verify_tag(self.artifact)
        self.assertIn("not published", str(ctx.exception))

    def test_unreachable_registry_fails_closed(self) -> None:
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            with self.assertRaises(ir.RegistryError):
                ir.verify_tag(self.artifact)

    def test_pinned_reference_omits_the_tag(self) -> None:
        """The tag has done its job by this point; including it would suggest
        it is being trusted for resolution."""
        ref = ir.pinned_reference(self.artifact)
        self.assertTrue(ref.endswith(f"@{_A}"))
        self.assertNotIn("0.14.2-arm64", ref)

    def test_url_with_no_tag_is_rejected(self) -> None:
        """An oci url must carry a tag to verify against; a bare repo cannot."""
        bare = pa.Artifact(
            image="x", format="oci", platform="linux/arm64",
            url="gitea.dideric.is/didericis/x", digest=_A, version="1",
        )
        with self.assertRaises(ir.RegistryError):
            ir.pinned_reference(bare)

    def test_resolve_can_skip_verification_but_stays_pinned(self) -> None:
        """Offline use gives up detection, never integrity."""
        got = ir.resolve(self.artifact, verify=False)
        self.assertIn("@sha256:", got.pinned_ref)


class RegistryPlumbing(unittest.TestCase):
    def setUp(self) -> None:
        self.artifact = pa.resolve(
            pa.parse_artifacts_manifest(_entry()), "bot-bottle-gateway",
            prefer=("oci",), plat="linux/arm64",
        )

    def test_ref_without_a_registry_host_is_rejected(self) -> None:
        """`owner/name` is ambiguous — without a host there is nothing to ask
        what the tag resolves to."""
        with self.assertRaises(ir.RegistryError):
            ir._registry_host("didericis/bot-bottle-gateway")

    def test_ref_without_a_repository_path_is_rejected(self) -> None:
        with self.assertRaises(ir.RegistryError):
            ir._repository("gitea.dideric.is")

    def test_pull_uses_the_digest_pinned_reference(self) -> None:
        resolved = ir.resolve(self.artifact, verify=False)
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            ir.pull(resolved)
        self.assertIn(resolved.pinned_ref, run.call_args.args[0])

    def test_pull_failure_never_falls_back_to_building(self) -> None:
        """Silently building what could not be pulled would run bytes nobody
        verified."""
        proc = mock.Mock(returncode=1, stdout="", stderr="denied")
        with mock.patch("subprocess.run", return_value=proc):
            with self.assertRaises(ir.RegistryError) as ctx:
                ir.pull(ir.resolve(self.artifact, verify=False))
        self.assertIn("denied", str(ctx.exception))

    def test_missing_runtime_is_named(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no docker")):
            with self.assertRaises(ir.RegistryError):
                ir.pull(ir.resolve(self.artifact, verify=False))

    def test_local_build_opt_in(self) -> None:
        """Building stays a supported path — it is the answer for a current or
        custom agent harness, since published agent images may lag."""
        with mock.patch.dict("os.environ", {"BOT_BOTTLE_INFRA_BUILD": "local"}):
            self.assertTrue(ir.local_build_requested())
        with mock.patch.dict("os.environ", {"BOT_BOTTLE_INFRA_BUILD": ""}):
            self.assertFalse(ir.local_build_requested())

    def test_auth_header_is_sent_when_a_token_is_set(self) -> None:
        with mock.patch.dict("os.environ", {ir.TOKEN_ENV: "secret-value"}):
            req = ir._request("https://example.test/v2/x/manifests/t")
        self.assertTrue(req.has_header("Authorization"))

    def test_non_404_http_error_is_distinguished_from_missing(self) -> None:
        err = urllib.error.HTTPError("u", 500, "boom", None, None)  # type: ignore[arg-type]
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ir.RegistryError) as ctx:
                ir.verify_tag(self.artifact)
        self.assertNotIn("not published", str(ctx.exception))


class Ext4Fetch(unittest.TestCase):
    """The consume side for ext4. `pull` delegates verification to the container
    runtime; a generic package has no runtime to delegate to, so the download,
    the digest check and the cache all live here."""

    def _artifact(self, payload: bytes) -> "tuple[pa.Artifact, str]":
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        return pa.Artifact(
            image="bot-bottle-gateway", format="ext4", platform="linux/arm64",
            url=_EXT4_URL, digest=digest, version="0.14.2",
        ), digest

    def _serve(self, payload: bytes):
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(payload)
        return mock.patch("urllib.request.urlopen", return_value=resp)

    def test_downloads_and_verifies(self) -> None:
        payload = b"rootfs-bytes" * 100
        artifact, _ = self._artifact(payload)
        with tempfile.TemporaryDirectory() as tmp:
            with self._serve(payload):
                got = ir.ensure_ext4(artifact, cache_root=Path(tmp))
            self.assertTrue(got.is_file())
            self.assertEqual(got.read_bytes(), payload)

    def test_second_call_uses_the_cache(self) -> None:
        payload = b"cached" * 50
        artifact, _ = self._artifact(payload)
        with tempfile.TemporaryDirectory() as tmp:
            with self._serve(payload) as opened:
                ir.ensure_ext4(artifact, cache_root=Path(tmp))
                first = opened.call_count
                ir.ensure_ext4(artifact, cache_root=Path(tmp))
                self.assertEqual(opened.call_count, first, "re-downloaded")

    def test_digest_mismatch_fails_closed_and_removes_the_file(self) -> None:
        """A rootfs that failed verification must not be left where the next
        run would take it for cached-and-checked."""
        artifact, _ = self._artifact(b"expected")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._serve(b"something else entirely"):
                with self.assertRaises(ir.RegistryError) as ctx:
                    ir.ensure_ext4(artifact, cache_root=root)
            self.assertIn("verification", str(ctx.exception))
            self.assertEqual(list(root.rglob("*.gz")), [])

    def test_unpublished_version_says_how_to_fix_it(self) -> None:
        artifact, _ = self._artifact(b"x")
        err = urllib.error.HTTPError("u", 404, "nf", None, None)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(ir.RegistryError) as ctx:
                    ir.ensure_ext4(artifact, cache_root=Path(tmp))
        self.assertIn("publish_images", str(ctx.exception))

    def test_rejects_a_non_ext4_artifact(self) -> None:
        oci = pa.resolve(pa.parse_artifacts_manifest(_entry()),
                         "bot-bottle-gateway", prefer=("oci",), plat="linux/arm64")
        with self.assertRaises(ir.RegistryError):
            ir.ensure_ext4(oci)

    def test_url_is_the_baked_in_manifest_url(self) -> None:
        artifact, _ = self._artifact(b"x")
        url = ir.ext4_url(artifact)
        self.assertEqual(url, _EXT4_URL)
        self.assertIn("/0.14.2/", url)
        self.assertIn("rootfs.arm64.ext4.gz", url)
        self.assertIn("bot-bottle-gateway-rootfs", url)


class Interchangeability(unittest.TestCase):
    def _built(self, oci_hash: str, ext4_hash: str) -> pi.BuiltImage:
        return pi.BuiltImage(name="bot-bottle-gateway", version="0.14.2", artifacts=[
            pi.BuiltArtifact("oci", "linux/arm64", _A, "0.14.2-arm64",
                             "gitea.dideric.is/didericis/bot-bottle-gateway", oci_hash),
            pi.BuiltArtifact("ext4", "linux/arm64", _B, "rootfs.arm64.ext4.gz",
                             "bot-bottle-gateway-rootfs", ext4_hash),
        ])

    def test_matching_inputs_publish(self) -> None:
        pi.check_interchangeable(self._built("9f2c1e4b7a83d10c", "9f2c1e4b7a83d10c"))

    def test_drifted_inputs_abort_the_publish(self) -> None:
        """The manifest records one inputs_hash per image and so cannot express
        drift — which means it cannot catch it either. The publisher is the
        only place that sees both builds."""
        with self.assertRaises(pi.PublishError) as ctx:
            pi.check_interchangeable(self._built("9f2c1e4b7a83d10c", "ffffffffffffffff"))
        self.assertIn("not interchangeable", str(ctx.exception))

    def test_manifest_entry_round_trips_through_the_parser(self) -> None:
        """What the publisher writes must be what the consumer can read — and
        the flat keys must match their type/platform fields."""
        built = self._built("9f2c1e4b7a83d10c", "9f2c1e4b7a83d10c")
        entries = pa.parse_artifacts_manifest({built.name: pi.artifacts_manifest_entry(built)})
        self.assertEqual(len(entries[built.name].artifacts), 2)


class ManifestFile(unittest.TestCase):
    def test_missing_manifest_is_a_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pa.ArtifactsManifestError) as ctx:
                pa.load_artifacts_manifest(Path(tmp))
            self.assertIn("publish images first", str(ctx.exception))

    def test_round_trip_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / pa.ARTIFACTS_MANIFEST_NAME).write_text(json.dumps(_entry()))
            entries = pa.load_artifacts_manifest(root)
            self.assertIn("bot-bottle-gateway", entries)


class Ext4CacheKey(unittest.TestCase):
    """The cache is content-addressed. A hit returns before anything is
    compared to the manifest, so a path that ignored the digest would serve a
    previous download forever once a version was republished."""

    def _artifact(self, payload: bytes) -> pa.Artifact:
        return pa.Artifact(
            image="img", format="ext4", platform="linux/arm64",
            digest="sha256:" + hashlib.sha256(payload).hexdigest(),
            url=("https://gitea.dideric.is/api/packages/didericis/generic/"
                 "img-rootfs/0.0.0-dev/rootfs.arm64.ext4.gz"),
            version="0.0.0-dev",
        )

    def test_republished_version_is_not_served_from_the_old_cache(self) -> None:
        old, new = b"first-publish", b"second-publish"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for payload in (old, new):
                resp = mock.MagicMock()
                resp.__enter__.return_value = io.BytesIO(payload)
                with mock.patch("urllib.request.urlopen", return_value=resp):
                    got = ir.ensure_ext4(self._artifact(payload), cache_root=root)
                self.assertEqual(got.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
