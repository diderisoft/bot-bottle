"""Unit: the published-image asset manifest, hashing, and registry verification.

These lock the properties the whole publish/pull scheme rests on:

* a digest is verified, a tag is only *checked* — and a moved tag is caught;
* two formats of one image are interchangeable, enforced at publish time;
* `inputs_hash` distinguishes architectures, which the older firecracker
  version hash did not (two arches on one checkout would collide, and a host
  could pass verification on a rootfs it cannot boot).

No network and no container runtime: HTTP is mocked at `urlopen`.
"""

# pylint: disable=protected-access
# Reference-parsing helpers are private but carry real failure modes (a ref with
# no registry host has nothing to ask about a tag), so they are exercised
# directly rather than only through the paths that happen to call them.

from __future__ import annotations

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


def _entry(**over: object) -> dict[str, Any]:
    data = {
        "version": "0.14.2",
        "inputs_hash": "9f2c1e4b7a83d10c",
        "artifacts": {
            "oci": {
                "ref": "gitea.dideric.is/didericis/bot-bottle-gateway",
                "platforms": {"linux/arm64": {"tag": "0.14.2-arm64", "digest": _A}},
            },
            "ext4": {
                "package": "bot-bottle-gateway-rootfs",
                "platforms": {"linux/arm64": {"file": "rootfs.arm64.ext4.gz",
                                              "digest": _B}},
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

    def test_rejects_short_digest(self) -> None:
        bad = _entry()
        plats = bad["bot-bottle-gateway"]["artifacts"]["oci"]["platforms"]
        plats["linux/arm64"]["digest"] = "sha256:abc"
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
        plats = bad["bot-bottle-gateway"]["artifacts"]["oci"]["platforms"]
        plats["linux/riscv64"] = plats.pop("linux/arm64")
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

    def test_unknown_format_is_reported(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["squashfs"] = {"ref": "r", "platforms": {}}
        self.assertTrue(any("squashfs" in p for p in self._problems(bad)))

    def test_format_must_be_an_object(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["oci"] = "nope"
        self.assertTrue(self._problems(bad))

    def test_oci_needs_a_ref_and_ext4_needs_a_package(self) -> None:
        bad = _entry()
        del bad["bot-bottle-gateway"]["artifacts"]["oci"]["ref"]
        del bad["bot-bottle-gateway"]["artifacts"]["ext4"]["package"]
        problems = self._problems(bad)
        self.assertTrue(any("ref" in p for p in problems))
        self.assertTrue(any("package" in p for p in problems))

    def test_platforms_must_be_a_non_empty_object(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["oci"]["platforms"] = {}
        self.assertTrue(any("platforms" in p for p in self._problems(bad)))

    def test_platform_entry_must_be_an_object(self) -> None:
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"]["oci"]["platforms"]["linux/arm64"] = 7
        self.assertTrue(self._problems(bad))

    def test_missing_locator_is_reported(self) -> None:
        bad = _entry()
        del bad["bot-bottle-gateway"]["artifacts"]["oci"]["platforms"]["linux/arm64"]["tag"]
        self.assertTrue(any("tag" in p for p in self._problems(bad)))

    def test_missing_digest_is_reported(self) -> None:
        bad = _entry()
        del bad["bot-bottle-gateway"]["artifacts"]["ext4"]["platforms"]["linux/arm64"]["digest"]
        self.assertTrue(any("digest" in p for p in self._problems(bad)))

    def test_entry_with_no_valid_artifact_is_dropped(self) -> None:
        """An entry whose artifacts were all rejected is not half-loaded — a
        launch host must never resolve a partially-parsed entry."""
        bad = _entry()
        bad["bot-bottle-gateway"]["artifacts"] = {"oci": {"ref": "r", "platforms": {
            "linux/arm64": {"tag": "t", "digest": "sha256:short"}}}}
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
        del only_oci["bot-bottle-gateway"]["artifacts"]["ext4"]
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
        dupe["bot-bottle-gateway"]["artifacts"]["ext4"]["platforms"]["linux/arm64"]["digest"] = _A
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
        manifest's platform keys and the per-arch tags and filenames they carry
        — which is what closes the hole in the older firecracker version hash,
        where architecture appeared in neither the hash nor the artifact path."""
        entries = pa.parse_artifacts_manifest(_entry())
        artifact = pa.resolve(entries, "bot-bottle-gateway",
                              prefer=("oci",), plat="linux/arm64")
        self.assertIn("arm64", artifact.locator)
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


class Interchangeability(unittest.TestCase):
    def _built(self, oci_hash: str, ext4_hash: str) -> pi.BuiltImage:
        return pi.BuiltImage(name="bot-bottle-gateway", version="0.14.2", artifacts=[
            pi.BuiltArtifact("oci", "linux/arm64", _A, "0.14.2-arm64", "ref", oci_hash),
            pi.BuiltArtifact("ext4", "linux/arm64", _B, "r.gz", "pkg", ext4_hash),
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
        """What the publisher writes must be what the consumer can read."""
        built = self._built("9f2c1e4b7a83d10c", "9f2c1e4b7a83d10c")
        entries = pa.parse_artifacts_manifest({built.name: pi.artifacts_manifest_entry(built)})
        self.assertEqual(len(entries[built.name].artifacts), 2)


class MergeManifests(unittest.TestCase):
    """Each architecture is built on the host that builds it natively, so each
    emits a manifest covering only its own platform. Combining them must not let
    either clobber the other."""

    def _part(self, plat: str, sfx: str, oci: str, ext4: str) -> dict[str, Any]:
        return {"bot-bottle-gateway": {
            "version": "0.14.2", "inputs_hash": "9f2c1e4b7a83d10c",
            "artifacts": {
                "oci": {"ref": "r/bot-bottle-gateway",
                        "platforms": {plat: {"tag": f"0.14.2-{sfx}", "digest": oci}}},
                "ext4": {"package": "bot-bottle-gateway-rootfs",
                         "platforms": {plat: {"file": f"rootfs.{sfx}.ext4.gz",
                                              "digest": ext4}}},
            }}}

    def _merge(self, *parts: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for i, part in enumerate(parts):
                p = root / f"part{i}.json"
                p.write_text(json.dumps(part))
                paths.append(p)
            out = root / "merged.json"
            pi.merge_artifacts_manifests(paths, out)
            return json.loads(out.read_text())

    def test_unions_platforms_across_parts(self) -> None:
        merged = self._merge(
            self._part("linux/arm64", "arm64", _A, _B),
            self._part("linux/amd64", "amd64", _C, "sha256:" + "d" * 64),
        )
        arts = merged["bot-bottle-gateway"]["artifacts"]
        self.assertEqual(sorted(arts["oci"]["platforms"]),
                         ["linux/amd64", "linux/arm64"])
        self.assertEqual(sorted(arts["ext4"]["platforms"]),
                         ["linux/amd64", "linux/arm64"])

    def test_merged_output_still_validates(self) -> None:
        merged = self._merge(
            self._part("linux/arm64", "arm64", _A, _B),
            self._part("linux/amd64", "amd64", _C, "sha256:" + "d" * 64),
        )
        entries = pa.parse_artifacts_manifest(merged)
        self.assertEqual(pa.check_consistency(entries), [])
        self.assertEqual(len(entries["bot-bottle-gateway"].artifacts), 4)

    def test_conflicting_parts_abort(self) -> None:
        """Two hosts built the same image/format/platform and disagreed.
        Picking either would be arbitrary, and the manifest is meant to be the
        one thing a launch host can trust."""
        with self.assertRaises(pi.PublishError):
            self._merge(
                self._part("linux/arm64", "arm64", _A, _B),
                self._part("linux/arm64", "arm64", _C, _B),
            )

    def test_version_disagreement_aborts(self) -> None:
        other = self._part("linux/amd64", "amd64", _C, "sha256:" + "d" * 64)
        other["bot-bottle-gateway"]["version"] = "0.14.3"
        with self.assertRaises(pi.PublishError):
            self._merge(self._part("linux/arm64", "arm64", _A, _B), other)


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


if __name__ == "__main__":
    unittest.main()
