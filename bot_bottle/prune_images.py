"""Prune published images from the Gitea registry per the retention policy.

- The delete set is every published version minus everything a
  `published-artifacts.json` on `main` or a release tag references, so a
  supported version can't be collected.
- Collection is by age and count, never by quota.
- OCI layers dedupe: every release is kept 12 months and the newest patch of
  each minor forever. ext4 rootfs don't: the last few releases are kept.
  Losing an old ext4 only makes firecracker convert the OCI image locally.

Dry-run unless `--apply`. Uses `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import image_inputs, publish_images, resources
from .published_artifacts import (
    ARTIFACTS_MANIFEST_NAME,
    ArtifactsManifestError,
    parse_artifacts_manifest,
)

_HTTP_TIMEOUT_SECONDS = 60.0
_PAGE_LIMIT = 50

# Gitea package types.
_TYPE_CONTAINER = "container"  # the OCI images
_TYPE_GENERIC = "generic"  # the ext4 rootfs tarballs

# Retention thresholds: the only knobs.
OCI_MAX_AGE = timedelta(days=365)  # every OCI release kept this long...
DEV_RC_MAX_AGE = timedelta(days=30)  # ...dev/RC tags only this long.
EXT4_KEEP_INFRA = 3  # last N ext4 releases for an infra image...
EXT4_KEEP_AGENT = 2  # ...current + previous for an agent image.

# Exactly major.minor.patch; pre-release suffixes fall under the dev/RC rule.
_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# Per-arch suffix on OCI tags and generic filenames.
_ARCHES = ("amd64", "arm64")

# Agent images may lag, so they keep fewer ext4 releases.
_AGENT_IMAGES = frozenset(image_inputs.IMAGES) - frozenset(image_inputs.INFRA_ROLES)


class PruneError(RuntimeError):
    """Listing or deleting failed. Fatal; a partial prune is safe to re-run."""


@dataclass(frozen=True)
class PackageVersion:
    """One deletable package version: a per-arch tag (`0.14.2-arm64`) for a
    container, or a version directory holding every arch (`0.14.2`) for a
    generic. So `platform` is set only for containers."""

    registry_type: str  # _TYPE_CONTAINER | _TYPE_GENERIC
    package: str  # Gitea package name
    image: str  # image name (package minus -rootfs)
    version: str  # registry version string
    base: str  # version without the -<arch> suffix
    platform: str | None  # amd64/arm64 for containers; None for generics
    created_at: datetime
    # Release triple, or None for a dev/RC version.
    release: tuple[int, int, int] | None

    @property
    def is_release(self) -> bool:
        return self.release is not None

    @property
    def identity(self) -> tuple[str, str, str]:
        """(type, package, version): what protection is keyed on."""
        return (self.registry_type, self.package, self.version)


@dataclass(frozen=True)
class Decision:
    """The verdict for one version, with its reason (shown in the plan)."""

    version: PackageVersion
    delete: bool
    reason: str


# --- Parsing registry versions ---

def _split_arch(base_with_arch: str) -> tuple[str, str | None]:
    """`0.14.2-arm64` -> (`0.14.2`, `arm64`); `0.14.2` -> (`0.14.2`, None).
    Only a known arch is stripped, so `0.0.0-dev-7` keeps its `-7`."""
    for arch in _ARCHES:
        suffix = f"-{arch}"
        if base_with_arch.endswith(suffix):
            return base_with_arch[: -len(suffix)], arch
    return base_with_arch, None


def _image_of(registry_type: str, package: str) -> str | None:
    """Our image name for a package (a generic is `<image>-rootfs`), or None if
    it isn't one of `image_inputs.IMAGES`."""
    if registry_type == _TYPE_GENERIC:
        if not package.endswith("-rootfs"):
            return None
        image = package[: -len("-rootfs")]
    else:
        image = package
    return image if image in image_inputs.IMAGES else None


def parse_version(
    registry_type: str, package: str, version: str, created_at: datetime,
) -> PackageVersion | None:
    """A raw registry version as a `PackageVersion`, or None (never delete) for
    one that isn't ours or doesn't parse."""
    image = _image_of(registry_type, package)
    if image is None:
        return None
    # Container versions are per-arch tags; generic versions are arch-free.
    if registry_type == _TYPE_CONTAINER:
        base, platform = _split_arch(version)
        if platform is None:
            # We always tag `<version>-<arch>`; anything else isn't ours.
            return None
    else:
        base, platform = version, None
    match = _RELEASE.match(base)
    release = (
        (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if match else None
    )
    return PackageVersion(
        registry_type=registry_type, package=package, image=image,
        version=version, base=base, platform=platform,
        created_at=created_at, release=release,
    )


def _parse_created_at(raw: str) -> datetime:
    """Gitea's RFC3339 `created_at` as an aware UTC datetime (naive values are
    UTC)."""
    text = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --- Enumerating what is published ---

def _request(url: str, token: str, *, method: str = "GET") -> urllib.request.Request:
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"token {token}")
    return req


def list_package_versions(
    base: str, owner: str, token: str,
) -> list[PackageVersion]:
    """Every version of every package this repo publishes, paginated. Versions
    that don't parse are dropped."""
    versions: list[PackageVersion] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"page": page, "limit": _PAGE_LIMIT})
        url = f"{base}/api/v1/packages/{owner}?{query}"
        try:
            with urllib.request.urlopen(
                _request(url, token), timeout=_HTTP_TIMEOUT_SECONDS,
            ) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise PruneError(
                f"listing packages failed (HTTP {e.code}) at {url}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PruneError(f"registry unreachable: {url} ({e})") from e
        if not isinstance(batch, list):
            raise PruneError(f"unexpected packages response at {url}: not a list")
        for item in batch:
            try:
                created = _parse_created_at(item.get("created_at", ""))
            except (ValueError, TypeError):
                # An undatable version is never a delete candidate.
                continue
            parsed = parse_version(
                item.get("type", ""), item.get("name", ""),
                item.get("version", ""), created,
            )
            if parsed is not None:
                versions.append(parsed)
        if len(batch) < _PAGE_LIMIT:
            return versions
        page += 1


# --- What the manifests protect ---

def _git_show(repo_root: Path, ref: str, path: str) -> str | None:
    """`git show <ref>:<path>`, or None if absent (older tags predate the
    manifest)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
            capture_output=True, text=True, check=False,
        )
    except OSError as e:
        raise PruneError(f"git not available to read manifests ({e})") from e
    if proc.returncode != 0:
        return None
    return proc.stdout


def _release_refs(repo_root: Path) -> list[str]:
    """`main` plus every `v*` tag in this checkout. `main` is tried under
    several spellings because CI checks out a detached tag."""
    refs: list[str] = []
    for candidate in ("origin/main", "main"):
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", candidate],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            refs.append(candidate)
            break
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "tag", "-l", "v*"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode == 0:
        refs.extend(tag for tag in proc.stdout.split() if tag)
    return refs


def protected_identities(repo_root: Path) -> set[tuple[str, str, str]]:
    """The `(type, package, version)` set no prune may delete: everything a
    manifest on `main` or a release tag references. Read from git, so local
    edits can't change it."""
    protected: set[tuple[str, str, str]] = set()
    for ref in _release_refs(repo_root):
        raw = _git_show(repo_root, ref, ARTIFACTS_MANIFEST_NAME)
        if not raw:
            continue
        try:
            entries = parse_artifacts_manifest(json.loads(raw))
        except (json.JSONDecodeError, ArtifactsManifestError) as e:
            # A present but unparseable manifest is corruption; skipping it
            # would drop its protection.
            raise PruneError(
                f"manifest on {ref!r} is present but unparseable ({e}); "
                f"refusing to prune with a ref's protection missing"
            ) from e
        for entry in entries.values():
            for artifact in entry.artifacts:
                if artifact.format == "oci":
                    tag = artifact.url.rsplit(":", 1)[-1]
                    protected.add((_TYPE_CONTAINER, artifact.image, tag))
                else:
                    package = publish_images.ext4_package(artifact.image)
                    protected.add((_TYPE_GENERIC, package, artifact.version))
    return protected


# --- The policy ---

def _newest_patch_per_minor(
    versions: list[PackageVersion],
) -> set[tuple[str, str | None, int, int, int]]:
    """`(image, platform, major, minor, patch)` of the newest patch per minor
    and platform: kept forever."""
    best: dict[tuple[str, str | None, int, int], PackageVersion] = {}
    for v in versions:
        if v.release is None:
            continue
        major, minor, _ = v.release
        key = (v.image, v.platform, major, minor)
        current = best.get(key)
        if current is None or v.release > current.release:  # type: ignore[operator]
            best[key] = v
    return {(v.image, v.platform, *v.release) for v in best.values()}  # type: ignore[misc]


def _ext4_keep_bases(versions: list[PackageVersion]) -> dict[str, set[str]]:
    """Per image, the ext4 release bases kept by count: `EXT4_KEEP_INFRA` for
    infra, `EXT4_KEEP_AGENT` for agents. Sorted by release triple."""
    by_image: dict[str, dict[str, tuple[int, int, int]]] = {}
    for v in versions:
        if v.registry_type != _TYPE_GENERIC or v.release is None:
            continue
        by_image.setdefault(v.image, {})[v.base] = v.release
    keep: dict[str, set[str]] = {}
    for image, bases in by_image.items():
        limit = EXT4_KEEP_AGENT if image in _AGENT_IMAGES else EXT4_KEEP_INFRA
        ranked = sorted(bases, key=lambda b, _b=bases: _b[b], reverse=True)
        keep[image] = set(ranked[:limit])
    return keep


def _verdict(
    v: PackageVersion,
    *,
    age: timedelta,
    protected: set[tuple[str, str, str]],
    keep_forever_oci: set[tuple[str, str | None, int, int, int]],
    keep_ext4_bases: dict[str, set[str]],
) -> Decision:
    """The verdict for one version and its reason. Protection wins; then the
    format's rule."""
    # pylint: disable=too-many-return-statements
    if v.identity in protected:
        return Decision(v, False, "referenced by a retained manifest")
    # `is None` narrows the type for the container branch.
    if v.release is None:
        if age <= DEV_RC_MAX_AGE:
            return Decision(v, False, f"dev/RC within {DEV_RC_MAX_AGE.days}d ({age.days}d old)")
        return Decision(v, True, f"dev/RC older than {DEV_RC_MAX_AGE.days}d ({age.days}d old)")
    if v.registry_type == _TYPE_CONTAINER:
        if (v.image, v.platform, *v.release) in keep_forever_oci:
            return Decision(v, False, "newest patch of its minor")
        if age <= OCI_MAX_AGE:
            return Decision(v, False, f"OCI within {OCI_MAX_AGE.days}d ({age.days}d old)")
        return Decision(
            v, True,
            f"OCI older than {OCI_MAX_AGE.days}d, superseded patch ({age.days}d old)")
    # ext4: kept by count, not age.
    limit = EXT4_KEEP_AGENT if v.image in _AGENT_IMAGES else EXT4_KEEP_INFRA
    if v.base in keep_ext4_bases.get(v.image, set()):
        return Decision(v, False, f"among the last {limit} ext4 releases")
    return Decision(v, True, f"ext4 beyond the last {limit} releases")


def plan(
    versions: list[PackageVersion],
    protected: set[tuple[str, str, str]],
    *,
    now: datetime | None = None,
) -> list[Decision]:
    """The verdict for every version. Pure, so the policy is testable without a
    registry."""
    if now is None:
        now = datetime.now(timezone.utc)
    keep_forever_oci = _newest_patch_per_minor(
        [v for v in versions if v.registry_type == _TYPE_CONTAINER]
    )
    keep_ext4_bases = _ext4_keep_bases(versions)
    return [
        _verdict(
            v, age=now - v.created_at, protected=protected,
            keep_forever_oci=keep_forever_oci, keep_ext4_bases=keep_ext4_bases,
        )
        for v in versions
    ]


# --- Applying ---

def delete_version(
    base: str, owner: str, token: str, version: PackageVersion,
) -> None:
    """DELETE one package version; a 404 is success."""
    url = (
        f"{base}/api/v1/packages/{owner}/{version.registry_type}/"
        f"{version.package}/{urllib.parse.quote(version.version, safe='')}"
    )
    try:
        with urllib.request.urlopen(
            _request(url, token, method="DELETE"), timeout=_HTTP_TIMEOUT_SECONDS,
        ):
            pass
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise PruneError(
            f"deleting {version.package} {version.version} failed (HTTP {e.code})"
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PruneError(
            f"registry unreachable deleting {version.package} {version.version} ({e})"
        ) from e


# --- CLI ---

@dataclass
class _Summary:
    kept: int = 0
    deleted: int = 0
    reclaimed_versions: list[str] = field(default_factory=list)


def _render(decisions: list[Decision], *, apply: bool) -> _Summary:
    summary = _Summary()
    for d in sorted(
        decisions, key=lambda d: (d.version.package, d.version.version)
    ):
        v = d.version
        if d.delete:
            summary.deleted += 1
            summary.reclaimed_versions.append(f"{v.package} {v.version}")
            verb = "DELETE" if apply else "would delete"
            print(f"  {verb}  {v.package:<32} {v.version:<20} — {d.reason}")
        else:
            summary.kept += 1
            print(f"  keep    {v.package:<32} {v.version:<20} — {d.reason}")
    return summary


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prune_images",
        description="Delete published images the retention policy no longer keeps.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete. Without it, the plan is printed and nothing is "
             "removed — the safe default for a destructive tool.",
    )
    parser.add_argument(
        "--owner", default=None,
        help="registry owner to prune under (default: the publish owner). Point "
             "at a scratch owner to rehearse against throwaway packages.",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="checkout whose manifests define what is protected (default: the "
             "build root). Must have main and the release tags fetched.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    base, default_owner, token = publish_images._config()  # pylint: disable=protected-access
    owner = args.owner or default_owner
    if not token:
        print(
            f"{publish_images.TOKEN_ENV} is not set; a token with write:package "
            f"on {owner} is required to list and delete packages",
            file=sys.stderr,
        )
        return 2

    repo_root = args.repo_root or resources.build_root()
    protected = protected_identities(repo_root)
    if not protected:
        # An empty protected set means no manifest was found (tags or main not
        # fetched); deleting against it would collect everything.
        print(
            "refusing to prune: no published-artifacts.json found on main or any "
            "release tag (is this a full checkout with tags fetched?). Nothing "
            "would be protected.",
            file=sys.stderr,
        )
        return 2
    print(f"protected by manifests: {len(protected)} artifact reference(s)")

    versions = list_package_versions(base, owner, token)
    print(f"published: {len(versions)} package version(s) for our images\n")

    decisions = plan(versions, protected)
    summary = _render(decisions, apply=args.apply)

    print()
    if not args.apply:
        print(
            f"dry run: {summary.deleted} version(s) would be deleted, "
            f"{summary.kept} kept. Re-run with --apply to delete."
        )
        return 0

    failures: list[str] = []
    for d in decisions:
        if not d.delete:
            continue
        try:
            delete_version(base, owner, token, d.version)
        except PruneError as e:
            failures.append(str(e))
    if failures:
        print(
            f"{len(failures)} deletion(s) failed:\n  " + "\n  ".join(failures),
            file=sys.stderr,
        )
        return 1
    print(f"deleted {summary.deleted} version(s), kept {summary.kept}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
