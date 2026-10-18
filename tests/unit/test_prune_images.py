"""Unit: the registry retention pruner.

The policy is where the risk lives — a wrong verdict either wastes storage or,
far worse, deletes something a supported version still resolves — so `plan()` is
pure and gets the bulk of the coverage here. The HTTP and git edges are mocked at
their boundary (`urlopen`, `subprocess.run`) and exercised only for the failure
handling that keeps a half-finished prune safe to re-run.
"""

# pylint: disable=protected-access
# The parsing and grouping helpers carry the policy's edge cases, so they are
# exercised directly rather than only through main().

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from bot_bottle import prune_images as pr

_NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
_DIGEST = "sha256:" + "a" * 64


def _mk(registry_type: str, package: str, version: str, days_old: int) -> pr.PackageVersion:
    parsed = pr.parse_version(
        registry_type, package, version, _NOW - timedelta(days=days_old),
    )
    assert parsed is not None, f"{package} {version} did not parse"
    return parsed


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "boom", {}, io.BytesIO(b""))  # type: ignore[arg-type]


class VersionParsing(unittest.TestCase):
    """A version string is classified before any policy runs; a misparse here
    silently misroutes it (a release treated as dev, an arch eaten as a
    pre-release), so the boundaries are pinned explicitly."""

    def test_release_container_splits_the_arch(self) -> None:
        v = _mk("container", "bot-bottle-gateway", "0.14.2-arm64", 1)
        self.assertEqual(v.base, "0.14.2")
        self.assertEqual(v.platform, "arm64")
        self.assertTrue(v.is_release)
        self.assertEqual(v.release, (0, 14, 2))

    def test_generic_has_no_arch(self) -> None:
        v = _mk("generic", "bot-bottle-claude-rootfs", "0.14.2", 1)
        self.assertEqual(v.image, "bot-bottle-claude")
        self.assertIsNone(v.platform)
        self.assertEqual(v.base, "0.14.2")

    def test_dev_suffix_is_not_a_release(self) -> None:
        v = _mk("container", "bot-bottle-codex", "0.0.0-dev-7-amd64", 1)
        self.assertEqual(v.base, "0.0.0-dev-7")
        self.assertEqual(v.platform, "amd64")
        self.assertFalse(v.is_release)
        self.assertIsNone(v.release)

    def test_foreign_package_is_left_alone(self) -> None:
        self.assertIsNone(pr.parse_version("container", "some-other-app", "1.0.0-amd64", _NOW))
        self.assertIsNone(pr.parse_version("generic", "unrelated", "1.0.0", _NOW))

    def test_generic_needs_the_rootfs_suffix(self) -> None:
        """A generic package that is not `<image>-rootfs` is not one of ours."""
        self.assertIsNone(pr.parse_version("generic", "bot-bottle-claude", "1.0.0", _NOW))

    def test_untagged_container_version_is_ignored(self) -> None:
        """An OCI version with no arch suffix is not something we published (our
        tags are always `<version>-<arch>`), so it is never a delete candidate."""
        self.assertIsNone(pr.parse_version("container", "bot-bottle-gateway", "0.14.2", _NOW))
        self.assertIsNone(
            pr.parse_version("container", "bot-bottle-gateway", "sha256:" + "0" * 64, _NOW)
        )


class CreatedAtParsing(unittest.TestCase):
    def test_zulu_and_offset_both_land_in_utc(self) -> None:
        z = pr._parse_created_at("2026-08-10T17:03:14Z")
        off = pr._parse_created_at("2026-08-10T13:03:14-04:00")
        self.assertEqual(z, off)
        self.assertEqual(z.tzinfo, timezone.utc)


class Policy(unittest.TestCase):
    """The retention rules, one property each. `plan()` is pure, so every case is
    just data in, verdicts out."""

    def _verdict(self, decisions: list[pr.Decision], package: str, version: str) -> pr.Decision:
        for d in decisions:
            if d.version.package == package and d.version.version == version:
                return d
        self.fail(f"no decision for {package} {version}")

    def test_protected_beats_every_other_rule(self) -> None:
        # An ext4 that the count rule would drop, but a manifest pins.
        v = _mk("generic", "bot-bottle-claude-rootfs", "0.1.0", 999)
        protected = {("generic", "bot-bottle-claude-rootfs", "0.1.0")}
        [d] = pr.plan([v], protected, now=_NOW)
        self.assertFalse(d.delete)
        self.assertIn("retained manifest", d.reason)

    def test_fresh_dev_kept_stale_dev_deleted(self) -> None:
        fresh = _mk("container", "bot-bottle-gateway", "0.0.0-dev-9-amd64", 5)
        stale = _mk("container", "bot-bottle-gateway", "0.0.0-dev-1-amd64", 45)
        decisions = pr.plan([fresh, stale], set(), now=_NOW)
        self.assertFalse(self._verdict(decisions, "bot-bottle-gateway", "0.0.0-dev-9-amd64").delete)
        self.assertTrue(self._verdict(decisions, "bot-bottle-gateway", "0.0.0-dev-1-amd64").delete)

    def test_newest_patch_of_a_minor_is_kept_forever(self) -> None:
        # Both ancient; only the higher patch of the 0.13 minor survives.
        old = _mk("container", "bot-bottle-gateway", "0.13.1-amd64", 800)
        new = _mk("container", "bot-bottle-gateway", "0.13.4-amd64", 800)
        decisions = pr.plan([old, new], set(), now=_NOW)
        self.assertTrue(self._verdict(decisions, "bot-bottle-gateway", "0.13.1-amd64").delete)
        keep = self._verdict(decisions, "bot-bottle-gateway", "0.13.4-amd64")
        self.assertFalse(keep.delete)
        self.assertIn("newest patch", keep.reason)

    def test_newest_patch_is_per_arch(self) -> None:
        """Each arch is its own tag, so each keeps its own newest patch — an
        amd64 winner must not shield an arm64 loser or vice versa."""
        amd = _mk("container", "bot-bottle-gateway", "0.13.4-amd64", 800)
        arm = _mk("container", "bot-bottle-gateway", "0.13.4-arm64", 800)
        decisions = pr.plan([amd, arm], set(), now=_NOW)
        self.assertFalse(self._verdict(decisions, "bot-bottle-gateway", "0.13.4-amd64").delete)
        self.assertFalse(self._verdict(decisions, "bot-bottle-gateway", "0.13.4-arm64").delete)

    def test_recent_oci_release_kept_by_age(self) -> None:
        # Not the newest patch (0.14.5 exists), but inside the 12-month window.
        recent = _mk("container", "bot-bottle-gateway", "0.14.2-amd64", 30)
        newer = _mk("container", "bot-bottle-gateway", "0.14.5-amd64", 10)
        decisions = pr.plan([recent, newer], set(), now=_NOW)
        kept = self._verdict(decisions, "bot-bottle-gateway", "0.14.2-amd64")
        self.assertFalse(kept.delete)
        self.assertIn("within", kept.reason)

    def test_old_superseded_oci_release_deleted(self) -> None:
        old = _mk("container", "bot-bottle-gateway", "0.14.2-amd64", 400)
        newer = _mk("container", "bot-bottle-gateway", "0.14.5-amd64", 400)
        decisions = pr.plan([old, newer], set(), now=_NOW)
        self.assertTrue(self._verdict(decisions, "bot-bottle-gateway", "0.14.2-amd64").delete)

    def test_agent_ext4_keeps_current_and_previous_only(self) -> None:
        vers = [
            _mk("generic", "bot-bottle-claude-rootfs", "0.14.2", 3),
            _mk("generic", "bot-bottle-claude-rootfs", "0.14.1", 6),
            _mk("generic", "bot-bottle-claude-rootfs", "0.14.0", 9),
        ]
        decisions = pr.plan(vers, set(), now=_NOW)
        self.assertFalse(self._verdict(decisions, "bot-bottle-claude-rootfs", "0.14.2").delete)
        self.assertFalse(self._verdict(decisions, "bot-bottle-claude-rootfs", "0.14.1").delete)
        self.assertTrue(self._verdict(decisions, "bot-bottle-claude-rootfs", "0.14.0").delete)

    def test_infra_ext4_keeps_three(self) -> None:
        vers = [
            _mk("generic", "bot-bottle-gateway-rootfs", "0.14.2", 3),
            _mk("generic", "bot-bottle-gateway-rootfs", "0.14.1", 6),
            _mk("generic", "bot-bottle-gateway-rootfs", "0.14.0", 9),
            _mk("generic", "bot-bottle-gateway-rootfs", "0.13.9", 12),
        ]
        decisions = pr.plan(vers, set(), now=_NOW)
        self.assertTrue(self._verdict(decisions, "bot-bottle-gateway-rootfs", "0.13.9").delete)
        for keep in ("0.14.2", "0.14.1", "0.14.0"):
            self.assertFalse(self._verdict(decisions, "bot-bottle-gateway-rootfs", keep).delete)

    def test_ext4_count_ranks_numerically_not_lexically(self) -> None:
        """`0.10.0` must outrank `0.9.0`; a string sort would drop the wrong one
        and keep bytes nobody wants while deleting the newest release."""
        vers = [
            _mk("generic", "bot-bottle-claude-rootfs", "0.10.0", 3),
            _mk("generic", "bot-bottle-claude-rootfs", "0.9.0", 6),
            _mk("generic", "bot-bottle-claude-rootfs", "0.8.0", 9),
        ]
        decisions = pr.plan(vers, set(), now=_NOW)
        self.assertFalse(self._verdict(decisions, "bot-bottle-claude-rootfs", "0.10.0").delete)
        self.assertTrue(self._verdict(decisions, "bot-bottle-claude-rootfs", "0.8.0").delete)


class ProtectedSet(unittest.TestCase):
    """The protected set is read from git manifests; its correctness is the load-
    bearing safety property, so the manifest-to-identity translation is pinned."""

    _MANIFEST = {
        "bot-bottle-gateway": {
            "version": "0.14.2",
            "inputs_hash": "9f2c1e4b7a83d10c",
            "protocol": 1,
            "artifacts": {
                "oci_linux-amd64": {
                    "type": "oci", "platform": "linux-amd64",
                    "url": "gitea.dideric.is/didericis/bot-bottle-gateway:0.14.2-amd64",
                    "digest": _DIGEST,
                },
                "ext4_linux-amd64": {
                    "type": "ext4", "platform": "linux-amd64",
                    "url": "https://gitea.dideric.is/api/packages/didericis/generic/"
                           "bot-bottle-gateway-rootfs/0.14.2/rootfs.amd64.ext4.gz",
                    "digest": _DIGEST,
                },
            },
        },
    }

    def test_manifest_protects_the_tag_and_the_generic_version(self) -> None:
        def fake_refs(_root: object) -> list[str]:
            return ["main"]

        def fake_show(_root: object, ref: str, _path: str) -> str | None:
            return json.dumps(self._MANIFEST) if ref == "main" else None

        with mock.patch.object(pr, "_release_refs", fake_refs), \
                mock.patch.object(pr, "_git_show", fake_show):
            protected = pr.protected_identities(object())  # type: ignore[arg-type]

        self.assertIn(("container", "bot-bottle-gateway", "0.14.2-amd64"), protected)
        self.assertIn(("generic", "bot-bottle-gateway-rootfs", "0.14.2"), protected)

    def test_missing_manifest_protects_nothing(self) -> None:
        def refs(_root: object) -> list[str]:
            return ["v0.1.0"]

        def show(_root: object, _ref: str, _path: str) -> str | None:
            return None

        with mock.patch.object(pr, "_release_refs", refs), \
                mock.patch.object(pr, "_git_show", show):
            self.assertEqual(pr.protected_identities(object()), set())  # type: ignore[arg-type]

    def test_malformed_manifest_is_fatal(self) -> None:
        """A committed manifest that is present but unparseable is corruption,
        not absence. Skipping it would drop that ref's protection and risk
        deleting a still-referenced artifact, so the prune aborts instead."""
        def refs(_root: object) -> list[str]:
            return ["main"]

        def show(_root: object, _ref: str, _path: str) -> str | None:
            return "{not json"

        with mock.patch.object(pr, "_release_refs", refs), \
                mock.patch.object(pr, "_git_show", show):
            with self.assertRaises(pr.PruneError):
                pr.protected_identities(object())  # type: ignore[arg-type]


class Listing(unittest.TestCase):
    """Pagination and the drop-what-we-don't-own filter, mocked at urlopen."""

    def _page(self, items: list[dict[str, str]]) -> mock.Mock:
        resp = mock.Mock()
        resp.read.return_value = json.dumps(items).encode()
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        return resp

    def test_paginates_until_a_short_page(self) -> None:
        full = [
            {"type": "generic", "name": "bot-bottle-claude-rootfs",
             "version": f"0.{i}.0", "created_at": "2026-01-01T00:00:00Z"}
            for i in range(pr._PAGE_LIMIT)
        ]
        tail = [
            {"type": "container", "name": "bot-bottle-gateway",
             "version": "0.14.2-amd64", "created_at": "2026-01-01T00:00:00Z"},
            {"type": "container", "name": "not-ours",
             "version": "1.0.0-amd64", "created_at": "2026-01-01T00:00:00Z"},
        ]
        with mock.patch("urllib.request.urlopen", side_effect=[
            self._page(full), self._page(tail),
        ]):
            versions = pr.list_package_versions("https://x", "didericis", "tok")
        # Two pages fetched, the foreign package dropped.
        self.assertEqual(len(versions), pr._PAGE_LIMIT + 1)
        self.assertNotIn("not-ours", {v.package for v in versions})

    def test_http_error_is_wrapped(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            with self.assertRaises(pr.PruneError):
                pr.list_package_versions("https://x", "o", "t")


class Deleting(unittest.TestCase):
    def _artifact(self) -> pr.PackageVersion:
        return _mk("generic", "bot-bottle-claude-rootfs", "0.1.0", 1)

    def test_delete_targets_the_typed_route(self) -> None:
        captured: dict[str, object] = {}

        # `timeout` must keep its name: delete_version passes it as a keyword.
        def fake_urlopen(req: object, timeout: float = 0) -> mock.Mock:  # pylint: disable=unused-argument
            captured["url"] = req.full_url  # type: ignore[attr-defined]
            captured["method"] = req.get_method()  # type: ignore[attr-defined]
            resp = mock.Mock()
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            return resp

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            pr.delete_version("https://x", "didericis", "tok", self._artifact())

        self.assertEqual(captured["method"], "DELETE")
        self.assertIn(
            "/api/v1/packages/didericis/generic/bot-bottle-claude-rootfs/0.1.0",
            captured["url"],  # type: ignore[arg-type]
        )

    def test_404_on_delete_is_success(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404)):
            pr.delete_version("https://x", "o", "t", self._artifact())  # no raise

    def test_other_error_on_delete_raises(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            with self.assertRaises(pr.PruneError):
                pr.delete_version("https://x", "o", "t", self._artifact())


class MainSafety(unittest.TestCase):
    """The whole-program guards: no token, and — the one that matters — refusing
    to run when nothing is protected, which would otherwise delete everything."""

    def test_no_token_returns_2(self) -> None:
        with mock.patch.object(pr.publish_images, "_config",
                               return_value=("https://x", "didericis", "")):
            self.assertEqual(pr.main([]), 2)

    def test_empty_protected_set_refuses(self) -> None:
        with mock.patch.object(pr.publish_images, "_config",
                               return_value=("https://x", "didericis", "tok")), \
                mock.patch.object(pr.resources, "build_root", return_value="/repo"), \
                mock.patch.object(pr, "protected_identities", return_value=set()), \
                mock.patch.object(pr, "list_package_versions") as listed:
            self.assertEqual(pr.main([]), 2)
        listed.assert_not_called()  # never even enumerated, let alone deleted

    def test_dry_run_deletes_nothing(self) -> None:
        vers = [_mk("generic", "bot-bottle-claude-rootfs", "0.1.0", 999)]
        with mock.patch.object(pr.publish_images, "_config",
                               return_value=("https://x", "didericis", "tok")), \
                mock.patch.object(pr.resources, "build_root", return_value="/repo"), \
                mock.patch.object(pr, "protected_identities",
                                  return_value={("generic", "x", "y")}), \
                mock.patch.object(pr, "list_package_versions", return_value=vers), \
                mock.patch.object(pr, "delete_version") as deleted:
            self.assertEqual(pr.main([]), 0)  # default: no --apply
        deleted.assert_not_called()

    def test_apply_deletes_the_stale_version(self) -> None:
        # A dev tag past the 30-day window deletes regardless of count.
        vers = [_mk("container", "bot-bottle-claude", "0.0.0-dev-1-amd64", 999)]
        with mock.patch.object(pr.publish_images, "_config",
                               return_value=("https://x", "didericis", "tok")), \
                mock.patch.object(pr.resources, "build_root", return_value="/repo"), \
                mock.patch.object(pr, "protected_identities",
                                  return_value={("generic", "other", "z")}), \
                mock.patch.object(pr, "list_package_versions", return_value=vers), \
                mock.patch.object(pr, "delete_version") as deleted:
            self.assertEqual(pr.main(["--apply"]), 0)
        deleted.assert_called_once()


if __name__ == "__main__":
    unittest.main()
