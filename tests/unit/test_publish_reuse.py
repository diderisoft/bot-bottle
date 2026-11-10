"""Unit: a publish reuses images whose inputs are already published, and
promotion keeps those entries as they are.

The registry and every build are mocked: what is pinned here is the decision
(reuse or rebuild), that a reused entry reaches the candidate and the release
manifests unchanged, and that promotion copies only what was newly built.
"""

# pylint: disable=protected-access

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle import publish_images as pi
from bot_bottle import publish_promote as pp
from bot_bottle import publish_reuse as pr
from bot_bottle.compat import PROTOCOL_VERSION
from bot_bottle.published_artifacts import parse_artifacts_manifest, resolve

_HASH = "300cb3deabf3744d"
_OTHER = "9f3cdd52f5ccf1df"
_PLATFORMS = ["linux/amd64", "linux/arm64"]
_GHCR = {"BOT_BOTTLE_INFRA_ARTIFACT_BASE": "https://ghcr.io",
         "BOT_BOTTLE_INFRA_ARTIFACT_OWNER": "diderisoft"}
_GITEA = {"BOT_BOTTLE_INFRA_ARTIFACT_BASE": "https://gitea.dideric.is",
          "BOT_BOTTLE_INFRA_ARTIFACT_OWNER": "didericis",
          pi.EXT4_STORE_ENV: "generic"}


def _digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def _release_entry(
    name: str, *, version: str = "0.1.8", inputs: str = _HASH,
    platforms: tuple[str, ...] = ("amd64", "arm64"),
    formats: tuple[str, ...] = ("oci", "ext4"),
    protocol: int = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """An entry as a GHCR release writes it."""
    artifacts: dict[str, Any] = {}
    seeds = iter("0123456789abcdef")
    for fmt in formats:
        for arch in platforms:
            repo = name if fmt == "oci" else f"{name}-rootfs"
            artifacts[f"{fmt}_linux-{arch}"] = {
                "type": fmt, "platform": f"linux-{arch}",
                "url": f"ghcr.io/diderisoft/{repo}:{version}-{arch}",
                "digest": _digest(next(seeds)),
            }
    return {"version": version, "inputs_hash": inputs, "protocol": protocol,
            "artifacts": artifacts}


def _dev_entry(name: str, version: str = "0.0.0-dev-42") -> dict[str, Any]:
    """An entry as the Gitea dev candidate writes it."""
    artifacts: dict[str, Any] = {}
    seeds = iter("fedcba9876543210")
    for arch in ("amd64", "arm64"):
        artifacts[f"oci_linux-{arch}"] = {
            "type": "oci", "platform": f"linux-{arch}",
            "url": f"gitea.dideric.is/didericis/{name}:{version}-{arch}",
            "digest": _digest(next(seeds)),
        }
        artifacts[f"ext4_linux-{arch}"] = {
            "type": "ext4", "platform": f"linux-{arch}",
            "url": (f"https://gitea.dideric.is/api/packages/didericis/generic/"
                    f"{name}-rootfs/{version}/rootfs.{arch}.ext4.gz"),
            "digest": _digest(next(seeds)),
        }
    return {"version": version, "inputs_hash": _OTHER, "protocol": PROTOCOL_VERSION,
            "artifacts": artifacts}


def _write(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _entry_block(name: str, entry: dict[str, Any]) -> str:
    """How `write_artifacts_manifest` lays one entry out inside the file."""
    return json.dumps({name: entry}, indent=2, sort_keys=True)[2:-2]


class _Captured(io.TextIOWrapper):
    """Captured stdout that, unlike StringIO, `main` can `reconfigure`."""

    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="utf-8")

    def getvalue(self) -> str:
        self.flush()
        buffer = self.buffer
        assert isinstance(buffer, io.BytesIO)
        return buffer.getvalue().decode("utf-8")


class _Quiet(unittest.TestCase):
    def setUp(self) -> None:
        self.out = _Captured()
        self.enterContext(contextlib.redirect_stdout(self.out))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))


class PlanReuse(_Quiet):
    """Reuse only when the published entry is a build of these inputs, in every
    format and platform asked for, and still in its registry."""

    def plan(self, source: dict[str, Any], *, local: str = _HASH,
             present: bool = True, no_ext4: bool = False,
             images: list[str] | None = None) -> dict[str, pr.ReusedImage]:
        path = _write(self.tmp / "release.json", source)
        with mock.patch.object(pi.image_inputs, "inputs_hash", return_value=local), \
             mock.patch.object(pr, "artifact_present", return_value=present):
            return pr.plan_reuse(images or list(source), _PLATFORMS, path,
                                 repo_root=self.tmp, no_ext4=no_ext4)

    def test_matching_hash_with_every_platform_and_format_is_reused(self) -> None:
        entry = _release_entry("bot-bottle-claude")
        reused = self.plan({"bot-bottle-claude": entry})
        self.assertEqual(["bot-bottle-claude"], list(reused))
        self.assertEqual(entry, reused["bot-bottle-claude"].entry)
        self.assertIn(f"reusing bot-bottle-claude from 0.1.8 (inputs {_HASH})",
                      self.out.getvalue())

    def test_a_different_hash_rebuilds(self) -> None:
        reused = self.plan({"bot-bottle-claude": _release_entry("bot-bottle-claude")},
                           local=_OTHER)
        self.assertEqual({}, reused)
        self.assertIn("rebuilding bot-bottle-claude: inputs", self.out.getvalue())

    def test_a_missing_platform_rebuilds(self) -> None:
        entry = _release_entry("bot-bottle-claude", platforms=("amd64",))
        self.assertEqual({}, self.plan({"bot-bottle-claude": entry}))
        self.assertIn("lacks", self.out.getvalue())
        self.assertIn("linux/arm64", self.out.getvalue())

    def test_a_missing_format_rebuilds(self) -> None:
        entry = _release_entry("bot-bottle-claude", formats=("oci",))
        self.assertEqual({}, self.plan({"bot-bottle-claude": entry}))
        self.assertIn("ext4", self.out.getvalue())

    def test_an_oci_only_publish_needs_no_ext4(self) -> None:
        entry = _release_entry("bot-bottle-claude", formats=("oci",))
        self.assertEqual(["bot-bottle-claude"],
                         list(self.plan({"bot-bottle-claude": entry}, no_ext4=True)))

    def test_another_protocol_rebuilds(self) -> None:
        """No agent image bakes `compat`, so its hash can't see a protocol bump."""
        entry = _release_entry("bot-bottle-claude", protocol=PROTOCOL_VERSION + 1)
        self.assertEqual({}, self.plan({"bot-bottle-claude": entry}))
        self.assertIn("protocol", self.out.getvalue())

    def test_an_image_gone_from_its_registry_rebuilds(self) -> None:
        self.assertEqual({}, self.plan(
            {"bot-bottle-claude": _release_entry("bot-bottle-claude")}, present=False))
        self.assertIn("gone from its registry", self.out.getvalue())

    def test_an_unreachable_registry_rebuilds(self) -> None:
        path = _write(self.tmp / "release.json",
                      {"bot-bottle-claude": _release_entry("bot-bottle-claude")})
        with mock.patch.object(pi.image_inputs, "inputs_hash", return_value=_HASH), \
             mock.patch.object(pr, "artifact_present",
                               side_effect=pi.PublishError("unreachable")):
            reused = pr.plan_reuse(["bot-bottle-claude"], _PLATFORMS, path,
                                   repo_root=self.tmp)
        self.assertEqual({}, reused)

    def test_an_image_absent_from_the_manifest_is_built(self) -> None:
        reused = self.plan({"bot-bottle-claude": _release_entry("bot-bottle-claude")},
                           images=["bot-bottle-claude", "bot-bottle-pi"])
        self.assertEqual(["bot-bottle-claude"], list(reused))

    def test_a_malformed_manifest_aborts(self) -> None:
        path = self.tmp / "release.json"
        path.write_text("{not json")
        with self.assertRaises(pi.PublishError):
            pr.plan_reuse(["bot-bottle-claude"], _PLATFORMS, path, repo_root=self.tmp)


class PublishWithReuse(_Quiet):
    """`main` builds only what changed and copies the rest into the candidate."""

    def setUp(self) -> None:
        super().setUp()
        self.release = {
            "bot-bottle-claude": _release_entry("bot-bottle-claude"),
            "bot-bottle-gateway": _release_entry("bot-bottle-gateway", inputs=_OTHER),
        }
        self.source = _write(self.tmp / "release.json", self.release)
        self.work = self.tmp / "candidate"
        hashes = {"bot-bottle-claude": _HASH, "bot-bottle-gateway": "1111111111111111"}
        self.enterContext(mock.patch.dict("os.environ", {pi.TOKEN_ENV: "tok", **_GITEA}))

        def inputs_hash(name: str, **_: object) -> str:
            return hashes[name]

        self.enterContext(mock.patch.object(
            pi.image_inputs, "inputs_hash", side_effect=inputs_hash))
        self.enterContext(mock.patch.object(pr, "artifact_present", return_value=True))
        self.enterContext(mock.patch.object(pi, "link_to_repo"))
        self.published = self.enterContext(
            mock.patch.object(pi, "already_published", return_value=[]))
        self.build = self.enterContext(mock.patch.object(
            pi, "build_all", side_effect=self._build))

    @staticmethod
    def _build(images: list[str], platforms: list[str], version: str,
               **_: object) -> list[pi.BuiltImage]:
        return [pi.BuiltImage(name=name, version=version, artifacts=[
            pi.BuiltArtifact("oci", plat, _digest(str(i)), pi.oci_tag(version, plat),
                             pi.oci_ref(name), "1111111111111111")
            for i, plat in enumerate(platforms)]) for name in images]

    def run_main(self, *extra: str) -> int:
        return pi.main(["--image", "bot-bottle-claude", "--image", "bot-bottle-gateway",
                        "--platform", "linux/amd64", "--platform", "linux/arm64",
                        "--version", "0.0.0-dev-42", "--force", "--no-ext4",
                        "--work-dir", str(self.work), *extra])

    def test_only_the_changed_image_is_built(self) -> None:
        self.assertEqual(0, self.run_main("--reuse-from", str(self.source)))
        self.assertEqual(["bot-bottle-gateway"], self.build.call_args.args[0])
        # The preflight only asks about what will be published.
        self.assertEqual(["bot-bottle-gateway"], self.published.call_args.args[0])

    def test_the_reused_entry_is_copied_byte_for_byte(self) -> None:
        self.run_main("--reuse-from", str(self.source))
        candidate = (self.work / pi.ARTIFACTS_MANIFEST_NAME).read_text()
        block = _entry_block("bot-bottle-claude", self.release["bot-bottle-claude"])
        self.assertIn(block, self.source.read_text())
        self.assertIn(block, candidate)
        data = json.loads(candidate)
        self.assertEqual(self.release["bot-bottle-claude"], data["bot-bottle-claude"])
        self.assertEqual("0.0.0-dev-42", data["bot-bottle-gateway"]["version"])

    def test_the_candidate_mixes_versions_and_still_resolves(self) -> None:
        self.run_main("--reuse-from", str(self.source))
        entries = parse_artifacts_manifest(
            json.loads((self.work / pi.ARTIFACTS_MANIFEST_NAME).read_text()))
        claude = resolve(entries, "bot-bottle-claude", prefer=("ext4",),
                         plat="linux/arm64")
        gateway = resolve(entries, "bot-bottle-gateway", prefer=("oci",),
                          plat="linux/arm64")
        self.assertEqual("0.1.8", claude.version)
        self.assertTrue(claude.url.startswith("ghcr.io/diderisoft/"))
        self.assertEqual("0.0.0-dev-42", gateway.version)

    def test_no_reuse_builds_everything(self) -> None:
        with mock.patch.object(pr, "plan_reuse") as plan:
            self.assertEqual(0, self.run_main("--reuse-from", str(self.source),
                                              "--no-reuse"))
        plan.assert_not_called()
        self.assertEqual(["bot-bottle-claude", "bot-bottle-gateway"],
                         self.build.call_args.args[0])

    def test_nothing_changed_builds_nothing(self) -> None:
        self.release["bot-bottle-gateway"] = _release_entry(
            "bot-bottle-gateway", inputs="1111111111111111")
        _write(self.source, self.release)
        self.assertEqual(0, self.run_main("--reuse-from", str(self.source)))
        self.build.assert_not_called()
        self.published.assert_not_called()
        self.assertEqual(self.source.read_text(),
                         (self.work / pi.ARTIFACTS_MANIFEST_NAME).read_text())

    def test_the_repo_root_manifest_is_the_default_source(self) -> None:
        with mock.patch.object(pi.resources, "build_root", return_value=self.tmp), \
             mock.patch.object(pr, "artifacts_manifest_path", return_value=self.source):
            self.run_main()
        self.assertEqual(["bot-bottle-gateway"], self.build.call_args.args[0])

    def test_a_missing_reuse_manifest_aborts(self) -> None:
        self.assertEqual(2, self.run_main("--reuse-from", str(self.tmp / "nope.json")))
        self.build.assert_not_called()


class ArtifactPresent(unittest.TestCase):
    """The reuse check asks each artifact's own registry, by digest."""

    def test_ghcr_is_asked_anonymously_from_a_gitea_publish(self) -> None:
        artifact = parse_artifacts_manifest(
            {"bot-bottle-claude": _release_entry("bot-bottle-claude")}
        )["bot-bottle-claude"].for_format("oci", "linux/arm64")
        assert artifact is not None
        with mock.patch.dict("os.environ", {pi.TOKEN_ENV: "gitea-tok", **_GITEA}), \
             mock.patch.object(pi.oci_registry, "Registry") as registry:
            registry.return_value.manifest_exists.return_value = True
            self.assertTrue(pr.artifact_present(artifact))
        self.assertEqual("ghcr.io", registry.call_args.args[0])
        self.assertEqual("", registry.call_args.kwargs["token"])
        registry.return_value.manifest_exists.assert_called_once_with(
            "diderisoft/bot-bottle-claude", artifact.digest)

    def test_an_oci_rootfs_is_asked_for_its_layer(self) -> None:
        artifact = parse_artifacts_manifest(
            {"bot-bottle-claude": _release_entry("bot-bottle-claude")}
        )["bot-bottle-claude"].for_format("ext4", "linux/amd64")
        assert artifact is not None
        with mock.patch.dict("os.environ", _GITEA), \
             mock.patch.object(pi.oci_registry, "Registry") as registry:
            registry.return_value.blob_exists.return_value = False
            self.assertFalse(pr.artifact_present(artifact))
        registry.return_value.blob_exists.assert_called_once_with(
            "diderisoft/bot-bottle-claude-rootfs", artifact.digest)

    def test_a_generic_package_is_asked_by_url(self) -> None:
        artifact = parse_artifacts_manifest(
            {"bot-bottle-claude": _dev_entry("bot-bottle-claude")}
        )["bot-bottle-claude"].for_format("ext4", "linux/amd64")
        assert artifact is not None
        with mock.patch.dict("os.environ", {pi.TOKEN_ENV: "gitea-tok", **_GITEA}), \
             mock.patch.object(pi, "_head", return_value=200) as head:
            self.assertTrue(pr.artifact_present(artifact))
        self.assertEqual((artifact.url, "gitea-tok"), head.call_args.args)

    def test_an_unreachable_registry_is_an_error_not_an_absence(self) -> None:
        artifact = parse_artifacts_manifest(
            {"bot-bottle-claude": _dev_entry("bot-bottle-claude")}
        )["bot-bottle-claude"].for_format("ext4", "linux/amd64")
        assert artifact is not None
        with mock.patch.dict("os.environ", _GITEA), \
             mock.patch.object(pi, "_head", return_value=0):
            with self.assertRaises(pi.PublishError):
                pr.artifact_present(artifact)


class PromoteWithReuse(_Quiet):
    """Promotion copies only the newly built entries; reused ones already on
    GHCR ship as they are."""

    def setUp(self) -> None:
        super().setUp()
        self.tested = {
            "bot-bottle-claude": _release_entry("bot-bottle-claude"),
            "bot-bottle-gateway": _dev_entry("bot-bottle-gateway"),
        }
        self.manifest = _write(self.tmp / "tested.json", self.tested)
        self.enterContext(mock.patch.dict("os.environ", {pi.TOKEN_ENV: "tok", **_GHCR}))
        self.copied: list[str] = []

        def copy(artifact: Any, *_: object, **__: object) -> str:
            self.copied.append(f"{artifact.image} {artifact.format} {artifact.platform}")
            return artifact.digest

        self.enterContext(mock.patch.object(pp, "_promote_oci", side_effect=copy))
        self.enterContext(mock.patch.object(pp, "_promote_ext4", side_effect=copy))
        self.present = self.enterContext(
            mock.patch.object(pr, "artifact_present", return_value=True))

    def test_reused_entries_are_kept_and_new_ones_promoted(self) -> None:
        promoted = pp.promote(self.manifest, "0.1.9", "tok")
        by_name = {p.name: p for p in promoted}
        kept = by_name["bot-bottle-claude"]
        assert isinstance(kept, pr.ReusedImage)
        self.assertEqual(self.tested["bot-bottle-claude"], kept.entry)
        self.assertIsInstance(by_name["bot-bottle-gateway"], pi.BuiltImage)
        self.assertEqual(4, len(self.copied))
        self.assertTrue(all(c.startswith("bot-bottle-gateway") for c in self.copied))
        # The kept entry was confirmed on GHCR by digest, every artifact of it.
        checked = {a.image for a in (c.args[0] for c in self.present.call_args_list)}
        self.assertEqual({"bot-bottle-claude"}, checked)
        self.assertEqual(4, self.present.call_count)

    def test_the_release_manifest_mixes_both(self) -> None:
        dest = self.tmp / "released.json"
        pi.write_artifacts_manifest(pp.promote(self.manifest, "0.1.9", "tok"), dest)
        text = dest.read_text()
        self.assertIn(_entry_block("bot-bottle-claude", self.tested["bot-bottle-claude"]),
                      text)
        gateway = json.loads(text)["bot-bottle-gateway"]
        self.assertEqual("0.1.9", gateway["version"])
        self.assertEqual("ghcr.io/diderisoft/bot-bottle-gateway:0.1.9-arm64",
                         gateway["artifacts"]["oci_linux-arm64"]["url"])
        self.assertEqual("ghcr.io/diderisoft/bot-bottle-gateway-rootfs:0.1.9-arm64",
                         gateway["artifacts"]["ext4_linux-arm64"]["url"])
        self.assertEqual(self.tested["bot-bottle-gateway"]["artifacts"]
                         ["oci_linux-arm64"]["digest"],
                         gateway["artifacts"]["oci_linux-arm64"]["digest"])

    def test_a_kept_entry_missing_from_the_registry_aborts(self) -> None:
        self.present.return_value = False
        with self.assertRaises(pi.PublishError) as ctx:
            pp.promote(self.manifest, "0.1.9", "tok")
        self.assertIn("no longer has", str(ctx.exception))

    def test_an_entry_elsewhere_is_promoted_even_if_its_hash_was_released(self) -> None:
        """Reuse is judged by where an entry lives, not by its hash."""
        self.tested["bot-bottle-claude"] = _dev_entry("bot-bottle-claude")
        _write(self.manifest, self.tested)
        promoted = pp.promote(self.manifest, "0.1.9", "tok")
        self.assertTrue(all(isinstance(p, pi.BuiltImage) for p in promoted))
        self.assertEqual(8, len(self.copied))

    def test_main_writes_the_release_version_beside_the_manifest(self) -> None:
        work = self.tmp / "released"
        with mock.patch.object(pi, "already_published", return_value=[]), \
             mock.patch.object(pi, "link_to_repo") as link:
            rc = pi.main(["--promote", str(self.manifest), "--version", "0.1.9",
                          "--work-dir", str(work)])
        self.assertEqual(0, rc)
        self.assertEqual("0.1.9\n", (work / pr.RELEASE_VERSION_NAME).read_text())
        # Only what was copied gets linked.
        self.assertEqual(["bot-bottle-gateway"], link.call_args.args[0])

    def test_in_publish_registry(self) -> None:
        entries = parse_artifacts_manifest(self.tested)
        self.assertTrue(all(pr.in_publish_registry(a)
                            for a in entries["bot-bottle-claude"].artifacts))
        self.assertFalse(any(pr.in_publish_registry(a)
                             for a in entries["bot-bottle-gateway"].artifacts))


class ManifestWriter(unittest.TestCase):
    def test_an_image_twice_aborts(self) -> None:
        reused = pr.ReusedImage("bot-bottle-claude", _release_entry("bot-bottle-claude"))
        built = pi.BuiltImage("bot-bottle-claude", "0.0.0-dev", artifacts=[
            pi.BuiltArtifact("oci", "linux/amd64", _digest("1"), "t", "r", _HASH)])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pi.PublishError):
                pi.write_artifacts_manifest([built, reused], Path(tmp) / "m.json")


if __name__ == "__main__":
    unittest.main()
