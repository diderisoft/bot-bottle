"""Prune published images from the Gitea registry per the retention policy
(PRD 0084, "Retention").

Publishing only ever adds. ext4 rootfs artifacts do not dedupe — each is a full
monolithic copy per release per arch — so a release cadence fills the registry
(and, unbounded, the build host) faster than a person notices. This reclaims the
space, and only the space, that no supported version still resolves.

Two rules shape the whole thing, both from the PRD:

- **Collection reads the manifests, never a hardcoded list.** The delete set is
  every published package version *minus* the union of `published-artifacts.json`
  across `main` and every retained release tag. That makes it structurally
  impossible to collect something a supported version still resolves — the one
  mistake that cannot be undone.
- **Trigger on time and count; never collect on quota.** Quota-driven deletion
  fires under pressure and removes whatever is oldest, precisely when there is
  least slack to reason about what is still in use. This tool takes no notice of
  how full the registry is; it deletes what the age/count policy says is stale
  and nothing else.

The asymmetry between formats is deliberate and comes straight from their cost
curves. OCI layers dedupe across versions, so a release costs tens of MB and time
is the right axis; every OCI release is kept for 12 months and the newest patch
of each minor is kept forever. An ext4 does not dedupe at all, so count is the
right axis; the last few releases are kept and older ones fall away. Deleting an
old rootfs only degrades a host to *slow* — the OCI image remains and the
firecracker consumer falls back to converting it locally, once, then cached — so
narrow ext4 retention is safe in a way narrow OCI retention would not be.

Dry-run by default: it prints the plan and deletes nothing. `--apply` — passed by
the scheduled job, withheld by a human eyeballing the plan — actually deletes.

Off-host, like `publish_images`: it needs the package-write token, not a launch
host. Auth reuses the same `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN`; a `write:package`
token can also list and delete.
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

# Gitea package types, as they appear in the packages API and its delete route.
_TYPE_CONTAINER = "container"  # the OCI images
_TYPE_GENERIC = "generic"      # the ext4 rootfs tarballs

# Retention thresholds. Named here rather than buried in the policy so the
# knobs — and the fact that they are the *only* knobs — are visible at a glance.
OCI_MAX_AGE = timedelta(days=365)      # every OCI release kept this long...
DEV_RC_MAX_AGE = timedelta(days=30)    # ...dev/RC tags only this long.
EXT4_KEEP_INFRA = 3                    # last N ext4 releases for an infra image...
EXT4_KEEP_AGENT = 2                    # ...current + previous for a convenience agent image.

# A release version is exactly major.minor.patch. Anything with a pre-release
# suffix (`-dev-7`, `-rc1`) is not a release and lives under the dev/RC rule.
_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# The per-arch suffix a published OCI tag / generic filename carries.
_ARCHES = ("amd64", "arm64")

# The agent (convenience) images, which are allowed to lag and are the most
# expensive thing to retain, so they keep fewer ext4 releases than infra images.
_AGENT_IMAGES = frozenset(image_inputs.IMAGES) - frozenset(image_inputs.INFRA_ROLES)


class PruneError(RuntimeError):
    """Listing or deleting failed. Fatal: a half-finished prune is fine to
    re-run, but a caller must not be told it collected when it did not."""


@dataclass(frozen=True)
class PackageVersion:
    """One deletable unit in the registry: a single package version.

    For a `container` package this is one per-arch *tag* (`0.14.2-arm64`); for a
    `generic` package it is one version directory (`0.14.2`) holding every arch's
    file. That is why `platform` is set only for containers — the generic delete
    is version-wide by construction, matching how the ext4s were published."""

    registry_type: str          # _TYPE_CONTAINER | _TYPE_GENERIC
    package: str                # the Gitea package name
    image: str                  # our image name (package minus the -rootfs suffix)
    version: str                # the registry version string, as deleted
    base: str                   # version with the -<arch> suffix stripped
    platform: str | None        # amd64/arm64 for containers; None for generics
    created_at: datetime
    # The parsed release triple, or None for a pre-release (dev/RC) version.
    release: tuple[int, int, int] | None

    @property
    def is_release(self) -> bool:
        return self.release is not None

    @property
    def identity(self) -> tuple[str, str, str]:
        """What the protected set is keyed on: (type, package, version). A
        version protected by a manifest is protected by this exact triple."""
        return (self.registry_type, self.package, self.version)


@dataclass(frozen=True)
class Decision:
    """The verdict for one package version, with the reason it was reached — the
    reason is the product here as much as the verb, since a human reads the plan
    before ever passing `--apply`."""

    version: PackageVersion
    delete: bool
    reason: str


# --------------------------------------------------------------------------- #
# Parsing registry versions into something the policy can reason about
# --------------------------------------------------------------------------- #

def _split_arch(base_with_arch: str) -> tuple[str, str | None]:
    """`0.14.2-arm64` -> (`0.14.2`, `arm64`); `0.14.2` -> (`0.14.2`, None).

    Only a trailing segment that is a known arch is stripped, so a pre-release
    like `0.0.0-dev-7` keeps its `-7` and is not mistaken for an architecture."""
    for arch in _ARCHES:
        suffix = f"-{arch}"
        if base_with_arch.endswith(suffix):
            return base_with_arch[: -len(suffix)], arch
    return base_with_arch, None


def _image_of(registry_type: str, package: str) -> str | None:
    """Our image name for a package, or None if it is not one of ours.

    A container package *is* the image; a generic package is `<image>-rootfs`.
    Anything outside `image_inputs.IMAGES` is left strictly alone — this tool
    only ever prunes what this repo publishes."""
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
    """Lift a raw registry version into a `PackageVersion`, or None to leave it
    untouched.

    None means "not something this tool understands", and the only safe response
    to that is to never delete it: a package that is not ours, or a version whose
    string does not parse (an untagged manifest digest, a hand-pushed oddity)."""
    image = _image_of(registry_type, package)
    if image is None:
        return None
    # A container version is a per-arch tag; a generic version is arch-free (the
    # arch lives in the filename inside it).
    if registry_type == _TYPE_CONTAINER:
        base, platform = _split_arch(version)
        if platform is None:
            # An OCI version with no arch suffix is not one we published (our
            # tags are always `<version>-<arch>`); do not touch it.
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
    """Gitea's RFC3339 `created_at` into an aware UTC datetime.

    `Z` is normalised to `+00:00` for `fromisoformat` on older interpreters, and
    a naive value is assumed UTC — the server's timezone — so age arithmetic is
    always between aware datetimes."""
    text = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Enumerating what is published
# --------------------------------------------------------------------------- #

def _request(url: str, token: str, *, method: str = "GET") -> urllib.request.Request:
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"token {token}")
    return req


def list_package_versions(
    base: str, owner: str, token: str,
) -> list[PackageVersion]:
    """Every version of every package this repo publishes, from the Gitea
    packages API.

    Paginated because an owner accumulates many versions; the loop stops on the
    first short page. Versions that do not parse (not ours, untagged, odd) are
    dropped here rather than carried through the policy as un-actionable."""
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
            parsed = parse_version(
                item.get("type", ""), item.get("name", ""),
                item.get("version", ""), _parse_created_at(item.get("created_at", "")),
            )
            if parsed is not None:
                versions.append(parsed)
        if len(batch) < _PAGE_LIMIT:
            return versions
        page += 1


# --------------------------------------------------------------------------- #
# What the manifests protect
# --------------------------------------------------------------------------- #

def _git_show(repo_root: Path, ref: str, path: str) -> str | None:
    """`git show <ref>:<path>`, or None if the ref or path is absent.

    Absent is expected and not an error: an old release tag predates the
    manifest, and a shallow clone may not have every ref."""
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
    """`main` plus every `v*` release tag that exists in this checkout.

    Every existing release tag is a *retained* release tag by definition, so its
    manifest protects. `main` is tried under a couple of spellings because CI
    checks out a detached tag and only has `origin/main`."""
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
    """The `(type, package, version)` set no prune may ever delete: everything
    referenced by `published-artifacts.json` on `main` or a release tag.

    Each OCI artifact protects its per-arch container tag; each ext4 artifact
    protects its generic version. Read from git rather than the working tree so
    an uncommitted local edit can neither add nor remove protection."""
    protected: set[tuple[str, str, str]] = set()
    for ref in _release_refs(repo_root):
        raw = _git_show(repo_root, ref, ARTIFACTS_MANIFEST_NAME)
        if not raw:
            continue
        try:
            entries = parse_artifacts_manifest(json.loads(raw))
        except (json.JSONDecodeError, ArtifactsManifestError):
            # A manifest that does not parse protects nothing extra; the CI
            # linter is what fails on a malformed committed manifest. Here the
            # conservative move is to skip it, not to abort the whole prune.
            continue
        for entry in entries.values():
            for artifact in entry.artifacts:
                if artifact.format == "oci":
                    tag = artifact.url.rsplit(":", 1)[-1]
                    protected.add((_TYPE_CONTAINER, artifact.image, tag))
                else:
                    package = publish_images.ext4_package(artifact.image)
                    protected.add((_TYPE_GENERIC, package, artifact.version))
    return protected


# --------------------------------------------------------------------------- #
# The policy
# --------------------------------------------------------------------------- #

def _newest_patch_per_minor(
    versions: list[PackageVersion],
) -> set[tuple[str, str | None, int, int, int]]:
    """The set of `(image, platform, major, minor, patch)` identities to keep
    forever — one per `(image, platform, major, minor)` group, its highest patch.

    "Newest patch of each minor, kept indefinitely" (OCI). Grouped by platform
    too because each arch is its own tag and must keep its own newest patch."""
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
    """Per image, the set of ext4 release *bases* to keep by count.

    Infra images keep the last `EXT4_KEEP_INFRA`; agent images, being convenience
    artifacts explicitly allowed to lag, keep only `EXT4_KEEP_AGENT`
    (current + previous). Sorted by the parsed release triple so `0.9.0` does not
    outrank `0.10.0` the way a string sort would."""
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
    """The retention verdict for one version, with its reason.

    Precedence is deliberate: protection wins over everything (it is the
    un-recoverable mistake), then format-specific rules decide the rest."""
    # pylint: disable=too-many-return-statements
    # One early return per policy rule reads better than the nested equivalent;
    # the rules are the point, and each is a single guarded line.
    if v.identity in protected:
        return Decision(v, False, "referenced by a retained manifest")
    # `is None` rather than `not is_release` so the release triple is narrowed
    # to non-None for the container branch below.
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
    # ext4 (generic): kept by count, not by age.
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
    """The verdict for every version — pure, so the whole policy is testable
    without a registry.

    The count-based keep-sets (newest OCI patch per minor, last-N ext4 releases)
    are computed once over the whole population, then each version is judged
    against them. A version this tool did not understand never reaches here — it
    was dropped at parse time — so every input gets a real verdict."""
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


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #

def delete_version(
    base: str, owner: str, token: str, version: PackageVersion,
) -> None:
    """DELETE one package version. A 404 is success — something already removed
    it, and the goal is that it be gone."""
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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

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
        # Every supported version is protected by a manifest; an empty set means
        # no manifest was found, which almost certainly means the tags/main were
        # not fetched. Deleting against an empty protected set would collect
        # everything — refuse rather than do that.
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
