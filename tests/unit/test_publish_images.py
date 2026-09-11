"""Unit: the off-host image publisher.

The publisher shells out to a container runtime and PUTs hundreds of MB to a
registry, so everything here is mocked at the `_run` / `urlopen` boundary. What
is exercised is the logic *around* those calls — how a version is derived, how
an artifact is addressed, and which failures abort a publish rather than
producing a half-published version a host could resolve.
"""

# pylint: disable=protected-access
# The private helpers here (_run, _config, _sha256, …) carry the failure
# handling worth pinning, so they are exercised directly rather than only
# through the callers that happen to reach them.

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle import publish_images as pi

_A = "sha256:" + "a" * 64
_HASH = "9f2c1e4b7a83d10c"


class Addressing(unittest.TestCase):
    """Names and URLs are derived, never hand-written, so a rename cannot leave
    the tag and the filename disagreeing about which artifact they mean."""

    def test_platform_suffix(self) -> None:
        self.assertEqual(pi._platform_suffix("linux/arm64"), "arm64")
        self.assertEqual(pi._platform_suffix("linux/amd64"), "amd64")

    def test_oci_ref_drops_the_scheme(self) -> None:
        ref = pi.oci_ref("bot-bottle-gateway")
        self.assertFalse(ref.startswith("https://"))
        self.assertTrue(ref.endswith("/bot-bottle-gateway"))

    def test_oci_tag_carries_the_arch(self) -> None:
        self.assertEqual(pi.oci_tag("0.14.2", "linux/arm64"), "0.14.2-arm64")

    def test_ext4_filename_carries_the_arch(self) -> None:
        """The arch has to be in the filename: without it, two architectures at
        one version resolve to the same URL, which is the collision the older
        firecracker artifact path had."""
        arm = pi.ext4_filename("linux/arm64")
        amd = pi.ext4_filename("linux/amd64")
        self.assertNotEqual(arm, amd)
        self.assertIn("arm64", arm)

    def test_ext4_url_is_versioned_and_per_arch(self) -> None:
        url = pi.ext4_url("bot-bottle-gateway", "0.14.2", "linux/amd64")
        self.assertIn("/0.14.2/", url)
        self.assertIn("amd64", url)
        self.assertIn(pi.ext4_package("bot-bottle-gateway"), url)

    def test_current_build_platform_is_a_known_platform(self) -> None:
        self.assertIn(pi.current_build_platform(), ("linux/arm64", "linux/amd64"))

    def test_config_honours_env_overrides(self) -> None:
        with mock.patch.dict("os.environ", {
            "BOT_BOTTLE_INFRA_ARTIFACT_BASE": "https://example.test/",
            "BOT_BOTTLE_INFRA_ARTIFACT_OWNER": "someone",
        }):
            base, owner, _ = pi._config()
        self.assertEqual(base, "https://example.test")  # trailing slash stripped
        self.assertEqual(owner, "someone")


class RepoVersion(unittest.TestCase):
    def test_reads_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
            self.assertEqual(pi.repo_version(root), "1.2.3")

    def test_missing_version_is_a_publish_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
            with self.assertRaises(pi.PublishError):
                pi.repo_version(root)


class RunHelper(unittest.TestCase):
    def test_returns_stdout_on_success(self) -> None:
        proc = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch("subprocess.run", return_value=proc):
            self.assertEqual(pi._run(["true"], what="x"), "ok")

    def test_nonzero_exit_aborts_with_the_output(self) -> None:
        proc = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch("subprocess.run", return_value=proc):
            with self.assertRaises(pi.PublishError) as ctx:
                pi._run(["false"], what="building x")
        self.assertIn("boom", str(ctx.exception))
        self.assertIn("building x", str(ctx.exception))

    def test_missing_binary_is_named(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no docker")):
            with self.assertRaises(pi.PublishError) as ctx:
                pi._run(["docker"], what="pushing")
        self.assertIn("not available", str(ctx.exception))


class FileHelpers(unittest.TestCase):
    def test_sha256_matches_hashlib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f"
            p.write_bytes(b"hello world")
            self.assertEqual(pi._sha256(p), hashlib.sha256(b"hello world").hexdigest())

    def test_gzip_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / "a", Path(tmp) / "a.gz"
            src.write_bytes(b"payload" * 100)
            pi._gzip(src, dest)
            with gzip.open(dest, "rb") as fh:
                self.assertEqual(fh.read(), b"payload" * 100)


class PushOci(unittest.TestCase):
    def test_returns_the_registry_digest(self) -> None:
        """The digest is read back from the registry, not computed locally —
        inventing it would defeat the point of pinning to it."""
        with mock.patch.object(pi, "_run", side_effect=["", "", f"ref@{_A}\n"]):
            got = pi.push_oci("bot-bottle-gateway", "0.14.2", "linux/arm64")
        self.assertEqual(got, _A)

    def test_missing_digest_aborts(self) -> None:
        with mock.patch.object(pi, "_run", side_effect=["", "", "no-digest-here"]):
            with self.assertRaises(pi.PublishError):
                pi.push_oci("bot-bottle-gateway", "0.14.2", "linux/arm64")


class PutGeneric(unittest.TestCase):
    def _body(self, tmp: str) -> Path:
        p = Path(tmp) / "rootfs.gz"
        p.write_bytes(b"x" * 32)
        return p

    def test_uploads(self) -> None:
        resp = mock.MagicMock()
        resp.__enter__.return_value = mock.Mock(status=201)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen", return_value=resp):
                pi.put_generic("https://x/y", self._body(tmp), "tok")

    def test_conflict_points_at_the_version(self) -> None:
        """A 409 means this version was already published. Overwriting silently
        would let two different builds answer to one version."""
        err = urllib.error.HTTPError("u", 409, "conflict", None, None)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(pi.PublishError) as ctx:
                    pi.put_generic("https://x/y", self._body(tmp), "tok")
        self.assertIn("already published", str(ctx.exception))

    def test_unreachable_registry_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.URLError("down")):
                with self.assertRaises(pi.PublishError):
                    pi.put_generic("https://x/y", self._body(tmp), "tok")


def _built(name: str = "bot-bottle-gateway") -> pi.BuiltImage:
    return pi.BuiltImage(name=name, version="0.14.2", artifacts=[
        pi.BuiltArtifact("oci", "linux/arm64", _A, "0.14.2-arm64", "ref", _HASH),
        pi.BuiltArtifact("ext4", "linux/arm64", "sha256:" + "b" * 64,
                         "rootfs.arm64.ext4.gz", "pkg", _HASH),
    ])


class WriteManifest(unittest.TestCase):
    def test_writes_sorted_stable_json(self) -> None:
        """The manifest is committed, so a churning key order would make every
        publish look like a change."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "published-artifacts.json"
            pi.write_artifacts_manifest([_built("b-img"), _built("a-img")], dest)
            data = json.loads(dest.read_text())
            self.assertEqual(list(data), ["a-img", "b-img"])
            self.assertEqual(data["a-img"]["inputs_hash"], _HASH)

    def test_entry_carries_both_formats(self) -> None:
        entry = pi.artifacts_manifest_entry(_built())
        artifacts: Any = entry["artifacts"]
        self.assertEqual(sorted(artifacts), ["ext4_linux-arm64", "oci_linux-arm64"])
        oci = artifacts["oci_linux-arm64"]
        self.assertEqual(oci["type"], "oci")
        self.assertEqual(oci["platform"], "linux-arm64")
        self.assertEqual(oci["url"], "ref:0.14.2-arm64")


class BuildImage(unittest.TestCase):
    def test_both_formats_share_one_inputs_hash(self) -> None:
        """Interchangeability is established by construction: both artifacts
        come from one build, so one hash is stamped onto both."""
        artifact = pi.BuiltArtifact("ext4", "linux/arm64", "sha256:" + "c" * 64,
                                    "r.gz", "pkg", _HASH)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(pi, "_run"), \
                 mock.patch.object(pi, "push_oci", return_value=_A), \
                 mock.patch.object(pi, "build_ext4", return_value=artifact) as ext4:
                produced = pi.build_image(
                    "bot-bottle-gateway", "0.14.2", "linux/arm64",
                    repo_root=Path("."), out_dir=Path(tmp))
        self.assertEqual([a.fmt for a in produced], ["oci", "ext4"])
        self.assertEqual(produced[0].inputs_hash, ext4.call_args.args[3])


class BuildExt4(unittest.TestCase):
    """Rootfs packaging delegates to the firecracker machinery rather than
    reimplementing it; what is asserted here is the wiring."""

    def _patched(self, tmp: Path) -> tuple[mock.Mock, mock.Mock]:
        fake_fc = mock.Mock()
        fake_infra = mock.Mock()
        fake_infra.role_init.return_value = "#!/bin/sh\n"

        def _mk_ext4(_base: Path, out: Path, **_kw: object) -> None:
            out.write_bytes(b"rootfs-bytes")

        fake_fc.build_base_rootfs_dir.return_value = tmp / "base"
        fake_fc.build_rootfs_ext4.side_effect = _mk_ext4
        return fake_fc, fake_infra

    def _run_build(self, image: str) -> tuple[pi.BuiltArtifact, mock.Mock, mock.Mock]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_fc, fake_infra = self._patched(root)
            mods = {"bot_bottle.backend.firecracker.infra_vm": fake_infra,
                    "bot_bottle.backend.firecracker.util": fake_fc}
            with mock.patch.dict("sys.modules", mods):
                with mock.patch(
                    "bot_bottle.backend.firecracker.infra_vm", fake_infra, create=True
                ), mock.patch(
                    "bot_bottle.backend.firecracker.util", fake_fc, create=True
                ):
                    artifact = pi.build_ext4(image, "linux/arm64", root / "o", _HASH,
                                         repo_root=Path("."))
            return artifact, fake_fc, fake_infra

    def test_infra_image_gets_its_role_init(self) -> None:
        """The infra pair boots a role-specific PID 1; an agent takes the
        default agent init, which is what init_script=None selects."""
        _, fake_fc, fake_infra = self._run_build("bot-bottle-gateway")
        fake_infra.role_init.assert_called_once_with("gateway")
        self.assertIsNotNone(fake_fc.build_base_rootfs_dir.call_args.kwargs["init_script"])

    def test_agent_image_takes_the_default_init(self) -> None:
        _, fake_fc, fake_infra = self._run_build("bot-bottle-claude")
        fake_infra.role_init.assert_not_called()
        self.assertIsNone(fake_fc.build_base_rootfs_dir.call_args.kwargs["init_script"])

    def test_intermediate_ext4_path_is_per_platform(self) -> None:
        """Both platforms of an image build concurrently. A shared intermediate
        path means two `mke2fs -d` runs populating one file, which corrupts it
        ("Directory block checksum does not match") only when their windows
        happen to overlap — so it passes until it doesn't."""
        paths: list[Path] = []
        for plat in ("linux/amd64", "linux/arm64"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fake_fc, fake_infra = self._patched(root)
                def _capture(_base: Path, out: Path, **_kw: object) -> None:
                    paths.append(out)
                    out.write_bytes(b"x")

                fake_fc.build_rootfs_ext4.side_effect = _capture
                with mock.patch(
                    "bot_bottle.backend.firecracker.infra_vm", fake_infra, create=True
                ), mock.patch(
                    "bot_bottle.backend.firecracker.util", fake_fc, create=True
                ):
                    pi.build_ext4("bot-bottle-gateway", plat, root / "o", _HASH,
                                  repo_root=Path("."))
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0].name, paths[1].name)

    def test_orchestrator_rootfs_gets_its_slack(self) -> None:
        """8192 MiB, not the 1024 default. The orchestrator builds agent images
        inside itself; at the default it boots and then runs out of space during
        an in-VM buildah build — invisible until a firecracker host tries it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_fc, fake_infra = self._patched(root)
            with mock.patch.object(pi, "_run"), mock.patch(
                "bot_bottle.backend.docker.util.pinned_local_image_ref",
                return_value="pinned",
            ), mock.patch(
                "bot_bottle.backend.firecracker.infra_vm", fake_infra, create=True
            ), mock.patch(
                "bot_bottle.backend.firecracker.util", fake_fc, create=True
            ):
                pi.build_ext4("bot-bottle-orchestrator", "linux/arm64",
                              root / "o", _HASH, repo_root=Path("."))
        self.assertEqual(
            fake_fc.build_rootfs_ext4.call_args.kwargs["slack_mib"], 8192)

    def test_derived_rootfs_gets_its_base_as_a_pinned_ref(self) -> None:
        """Dockerfile.orchestrator.fc declares ORCHESTRATOR_BASE_IMAGE with no
        default, so the FROM cannot silently resolve a stale `:latest`. The
        value has to be supplied, digest-pinned, or the build fails outright."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_fc, fake_infra = self._patched(root)
            with mock.patch.object(pi, "_run") as run, mock.patch(
                "bot_bottle.backend.docker.util.pinned_local_image_ref",
                return_value="bot-bottle-orchestrator:sha256-" + "a" * 64,
            ), mock.patch(
                "bot_bottle.backend.firecracker.infra_vm", fake_infra, create=True
            ), mock.patch(
                "bot_bottle.backend.firecracker.util", fake_fc, create=True
            ):
                pi.build_ext4("bot-bottle-orchestrator", "linux/arm64",
                              root / "o", _HASH, repo_root=Path("."))
        argv = " ".join(run.call_args_list[0].args[0])
        self.assertIn("ORCHESTRATOR_BASE_IMAGE=", argv)
        self.assertIn("sha256-" + "a" * 64, argv)

    def test_orchestrator_rootfs_comes_from_the_fc_dockerfile(self) -> None:
        """Its rootfs is Dockerfile.orchestrator.fc — FROM the OCI image, plus
        buildah. Exporting the plain OCI image yields a rootfs with no build
        tooling at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_fc, fake_infra = self._patched(root)
            with mock.patch.object(pi, "_run") as run, mock.patch(
                "bot_bottle.backend.docker.util.pinned_local_image_ref",
                return_value="pinned",
            ), mock.patch(
                "bot_bottle.backend.firecracker.infra_vm", fake_infra, create=True
            ), mock.patch(
                "bot_bottle.backend.firecracker.util", fake_fc, create=True
            ):
                pi.build_ext4("bot-bottle-orchestrator", "linux/arm64",
                              root / "o", _HASH, repo_root=Path("."))
        built = [" ".join(c.args[0]) for c in run.call_args_list]
        self.assertTrue(any("Dockerfile.orchestrator.fc" in b for b in built),
                        f"expected the .fc build, got {built}")
        # And the rootfs is exported from that derived image, not the OCI one.
        self.assertIn("-rootfs:build-arm64",
                      fake_fc.build_base_rootfs_dir.call_args.args[0])

    def test_gateway_reuses_its_oci_image_for_the_rootfs(self) -> None:
        """Only the orchestrator differs; everything else must not pay a second
        build."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_fc, fake_infra = self._patched(root)
            with mock.patch.object(pi, "_run") as run, mock.patch(
                "bot_bottle.backend.firecracker.infra_vm", fake_infra, create=True
            ), mock.patch(
                "bot_bottle.backend.firecracker.util", fake_fc, create=True
            ):
                pi.build_ext4("bot-bottle-gateway", "linux/arm64",
                              root / "o", _HASH, repo_root=Path("."))
        self.assertEqual(run.call_count, 0, "gateway should not rebuild")
        self.assertEqual(
            fake_fc.build_rootfs_ext4.call_args.kwargs["slack_mib"], 1024)

    def test_artifact_digest_covers_the_compressed_file(self) -> None:
        """The ext4 digest must cover the bytes that are actually published —
        the .gz — matching what the pull side verifies."""
        artifact, _, _ = self._run_build("bot-bottle-gateway")
        self.assertEqual(artifact.fmt, "ext4")
        self.assertTrue(artifact.digest.startswith("sha256:"))
        self.assertEqual(artifact.locator, pi.ext4_filename("linux/arm64"))
        self.assertEqual(artifact.inputs_hash, _HASH)


class BuildAll(unittest.TestCase):
    """Concurrency across (image, platform) pairs."""

    def test_builds_every_pair(self) -> None:
        with mock.patch.object(pi, "build_image", side_effect=self._artifacts_for) as b:
            built = pi.build_all(["a", "b"], ["linux/amd64", "linux/arm64"],
                                 "0.14.2", repo_root=Path("."),
                                 work_dir=Path("/tmp"), jobs=4)
        self.assertEqual(b.call_count, 4)
        self.assertEqual({e.name for e in built}, {"a", "b"})
        self.assertTrue(all(len(e.artifacts) == 2 for e in built))

    @staticmethod
    def _artifacts_for(image: str, version: str, plat: str, **_kw: object):
        del version
        return [pi.BuiltArtifact("oci", plat, "sha256:" + "a" * 64, "t", "ref", _HASH)]

    def test_every_failure_is_reported_not_just_the_first(self) -> None:
        """A publish that is going to fail should name every image that broke."""
        def boom(image: str, _version: str, _plat: str, **_kw: object):
            raise pi.PublishError(f"{image} exploded")

        with mock.patch.object(pi, "build_image", side_effect=boom):
            with self.assertRaises(pi.PublishError) as ctx:
                pi.build_all(["a", "b"], ["linux/amd64"], "0.14.2",
                             repo_root=Path("."), work_dir=Path("/tmp"), jobs=2)
        msg = str(ctx.exception)
        self.assertIn("a exploded", msg)
        self.assertIn("b exploded", msg)

    def test_local_tag_is_per_platform(self) -> None:
        """A shared `:latest` would race — whichever platform finished last
        would decide what got pushed and packaged."""
        self.assertNotEqual(pi.local_tag("img", "linux/amd64"),
                            pi.local_tag("img", "linux/arm64"))
        self.assertNotIn("latest", pi.local_tag("img", "linux/amd64"))

    def test_default_jobs_is_bounded(self) -> None:
        self.assertGreaterEqual(pi.default_jobs(), 1)
        self.assertLessEqual(pi.default_jobs(), 8)


class Preflight(unittest.TestCase):
    """Collisions are found before building, not at upload time.

    Uploads only start once every image is built, so a late collision throws
    away the whole matrix — ten minutes of work to learn what one round trip
    could have said up front."""

    def test_reports_existing_artifacts(self) -> None:
        with mock.patch.object(pi, "_head", return_value=200):
            found = pi.already_published(["img"], ["linux/amd64"], "0.14.2", "tok")
        # Both halves are checked: the ext4 URL and the OCI manifest.
        self.assertEqual(len(found), 2)

    def test_clean_version_reports_nothing(self) -> None:
        with mock.patch.object(pi, "_head", return_value=404):
            self.assertEqual(
                pi.already_published(["img"], ["linux/amd64"], "0.14.2", "tok"), [])

    def test_oci_is_checked_not_just_ext4(self) -> None:
        """`docker push` overwrites an existing tag silently, so the OCI half
        is the one nothing downstream would ever report."""
        calls: list[tuple[str, bool]] = []

        def head(url: str, _tok: str, *, basic: bool = False) -> int:
            calls.append((url, basic))
            return 200 if "/v2/" in url else 404

        with mock.patch.object(pi, "_head", side_effect=head):
            found = pi.already_published(["img"], ["linux/amd64"], "0.14.2", "tok")
        self.assertEqual(len(found), 1)
        self.assertIn("img", found[0])
        # The /v2/ check must use HTTP Basic: the Gitea REST `token` scheme is
        # rejected there with 401, which would misread an existing tag as absent.
        v2 = [c for c in calls if "/v2/" in c[0]]
        self.assertTrue(v2 and all(basic for _, basic in v2))

    def test_main_aborts_before_building(self) -> None:
        with mock.patch.dict("os.environ", {pi.TOKEN_ENV: "tok"}), \
             mock.patch.object(pi, "already_published", return_value=["x"]), \
             mock.patch.object(pi, "repo_version", return_value="0.14.2"), \
             mock.patch.object(pi, "build_all") as build:
            rc = pi.main(["--image", "img"])
        self.assertEqual(rc, 2)
        build.assert_not_called()

    def test_force_deletes_then_builds(self) -> None:
        with mock.patch.dict("os.environ", {pi.TOKEN_ENV: "tok"}), \
             mock.patch.object(pi, "already_published", return_value=["x"]), \
             mock.patch.object(pi, "repo_version", return_value="0.14.2"), \
             mock.patch.object(pi, "delete_published") as delete, \
             mock.patch.object(pi, "build_all", return_value=[]), \
             mock.patch.object(pi, "write_artifacts_manifest"):
            rc = pi.main(["--image", "img", "--force"])
        self.assertEqual(rc, 0)
        delete.assert_called_once()


class RepoLinking(unittest.TestCase):
    """Generic packages have no `org.opencontainers.image.source` equivalent,
    so they need an explicit call or they stay owner-scoped and invisible on
    the repo."""

    def test_links_both_containers_and_generic_packages(self) -> None:
        """The OCI source label alone is not enough — Gitea reads it when it
        creates a package, so a republish of an existing one stays unlinked."""
        resp = mock.MagicMock()
        with mock.patch("urllib.request.urlopen", return_value=resp) as opened:
            pi.link_to_repo(["a", "b"], "tok")
        urls = [c.args[0].full_url for c in opened.call_args_list]
        self.assertEqual(len(urls), 4)  # 2 images x {container, generic}
        self.assertEqual(sum("/container/" in u for u in urls), 2)
        self.assertEqual(sum("/generic/" in u for u in urls), 2)
        # Per package *name*, not per version — one call covers every version.
        self.assertTrue(all(u.endswith("/-/link/bot-bottle") for u in urls))

    def test_already_linked_is_not_a_warning(self) -> None:
        """Gitea answers 400 for an existing link, which is the steady state on
        every republish."""
        for code in (400, 409):
            err = urllib.error.HTTPError("u", code, "x", None, None)  # type: ignore[arg-type]
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with mock.patch("sys.stderr") as err_out:
                    pi.link_to_repo(["a"], "tok")
                self.assertFalse(err_out.write.called, f"warned on {code}")

    def test_failure_does_not_abort_the_publish(self) -> None:
        """Artifacts are already uploaded by this point — unlinked is harder to
        find, not broken, and not worth failing a release over."""
        err = urllib.error.HTTPError("u", 500, "boom", None, None)  # type: ignore[arg-type]
        with mock.patch("urllib.request.urlopen", side_effect=err):
            pi.link_to_repo(["a"], "tok")  # must not raise


class Promotion(unittest.TestCase):
    """Promotion copies; it never rebuilds. A rebuild at the release version
    ships bytes nobody tested, which is what the dev version exists to prevent."""

    def _manifest(self, tmp: Path) -> Path:
        p = tmp / "tested.json"
        p.write_text(json.dumps({"bot-bottle-gateway": {
            "version": "0.0.0-dev", "inputs_hash": _HASH,
            "artifacts": {
                "oci_linux-arm64": {
                    "type": "oci", "platform": "linux-arm64",
                    "url": "reg/o/bot-bottle-gateway:0.0.0-dev-arm64", "digest": _A},
                "ext4_linux-arm64": {
                    "type": "ext4", "platform": "linux-arm64",
                    "url": ("https://gitea.dideric.is/api/packages/didericis/generic/"
                            "bot-bottle-gateway-rootfs/0.0.0-dev/rootfs.arm64.ext4.gz"),
                    "digest": "sha256:" + "b" * 64},
            }}}))
        return p

    def test_copies_both_formats_and_never_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gz = root / "r.gz"
            gz.write_bytes(b"tested-bytes")
            digest = "sha256:" + hashlib.sha256(b"tested-bytes").hexdigest()
            manifest = self._manifest(root)
            # Make the ext4 digest match what the bytes actually hash to.
            manifest.write_text(manifest.read_text().replace("sha256:" + "b" * 64, digest))

            with mock.patch.object(pi, "_run", side_effect=["", "", "", f"r@{_A}\n"]), \
                 mock.patch.object(pi, "build_image") as build, \
                 mock.patch("bot_bottle.image_registry.ensure_ext4", return_value=gz), \
                 mock.patch.object(pi, "put_generic"):
                promoted = pi.promote(manifest, "1.0.0", "tok")
        build.assert_not_called()
        self.assertEqual(len(promoted), 1)
        self.assertEqual({a.fmt for a in promoted[0].artifacts}, {"oci", "ext4"})
        self.assertEqual(promoted[0].version, "1.0.0")

    def test_oci_is_pulled_by_digest_not_tag(self) -> None:
        """What gets re-tagged has to be exactly what was tested."""
        with tempfile.TemporaryDirectory():
            with mock.patch.object(pi, "_run", side_effect=["", "", "", f"r@{_A}\n"]) as run:
                pi._promote_oci(
                    mock.Mock(url="reg/o/img:0.0.0-dev-arm64", digest=_A,
                              platform="linux/arm64", image="img"),
                    "1.0.0")
            pulled = [c.args[0] for c in run.call_args_list if c.args[0][1] == "pull"]
        self.assertTrue(pulled)
        self.assertIn(f"reg/o/img@{_A}", pulled[0])

    def test_a_changed_digest_aborts(self) -> None:
        """If the promoted artifact does not hash to what passed, promotion has
        shipped something else — which defeats testing a dev version at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gz = root / "r.gz"
            gz.write_bytes(b"different")
            with mock.patch.object(pi, "_run", side_effect=["", "", "", f"r@{_A}\n"]), \
                 mock.patch("bot_bottle.image_registry.ensure_ext4", return_value=gz), \
                 mock.patch.object(pi, "put_generic"):
                with self.assertRaises(pi.PublishError) as ctx:
                    pi.promote(self._manifest(root), "1.0.0", "tok")
        self.assertIn("untested bytes", str(ctx.exception))


class Cli(unittest.TestCase):
    def test_publish_without_a_token_aborts_before_building(self) -> None:
        """An OCI push cannot be undone, so a missing token has to stop the run
        before anything is built rather than after."""
        with mock.patch.dict("os.environ", {pi.TOKEN_ENV: ""}), \
             mock.patch.object(pi, "build_image") as build:
            rc = pi.main([])
        self.assertEqual(rc, 2)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
