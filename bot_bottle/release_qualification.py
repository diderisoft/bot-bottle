"""Validated release and channel pointers for qualified commit bundles."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .release_manifest import ReleaseManifestError
from .release_publish import (
    HTTP_TIMEOUT_SECONDS,
    bundle_url,
    config,
    remote_bytes,
    request,
    upload,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_STABLE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_STAGING_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-rc\.(\d+)$")


def release_channel(tag: str) -> str:
    """Return the only channel eligible for *tag*."""
    if _STABLE_TAG_RE.fullmatch(tag):
        return "production"
    if _STAGING_TAG_RE.fullmatch(tag):
        return "staging"
    raise ReleaseManifestError(
        "release tag must be vX.Y.Z or vX.Y.Z-rc.N")


def release_version(tag: str) -> tuple[int, int, int, int]:
    """Return a monotonically comparable version tuple."""
    stable = _STABLE_TAG_RE.fullmatch(tag)
    if stable:
        return *(int(value) for value in stable.groups()), 1 << 30
    staging = _STAGING_TAG_RE.fullmatch(tag)
    if staging:
        return *(int(value) for value in staging.groups()[:3]), int(
            staging.group(4))
    raise ReleaseManifestError(
        "release tag must be vX.Y.Z or vX.Y.Z-rc.N")


def pointer_url(package: str, version: str, filename: str) -> str:
    base, owner, _ = config()
    return f"{base}/api/packages/{owner}/generic/{package}/{version}/{filename}"


def build_release_pointer(
    *,
    tag: str,
    source_commit: str,
    workflow_run: str,
    qualified_at: str,
) -> dict[str, Any]:
    """Build a qualified pointer to one immutable commit bundle."""
    channel = release_channel(tag)
    if _COMMIT_RE.fullmatch(source_commit) is None:
        raise ReleaseManifestError(
            "release source_commit must be 40 lowercase hex")
    if not workflow_run.strip() or not qualified_at.strip():
        raise ReleaseManifestError(
            "release qualification provenance is incomplete")
    return {
        "schema": 1,
        "qualified": True,
        "tag": tag,
        "channel": channel,
        "source_commit": source_commit,
        "bundle_index_url": bundle_url(source_commit, "bundle-index.json"),
        "qualification": {
            "workflow_run": workflow_run.strip(),
            "qualified_at": qualified_at.strip(),
        },
    }


def parse_release_pointer(data: Any) -> dict[str, Any]:
    """Validate a release or channel pointer."""
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ReleaseManifestError("release pointer must use schema 1")
    if data.get("qualified") is not True:
        raise ReleaseManifestError("release pointer must be qualified")
    tag = data.get("tag")
    channel = data.get("channel")
    commit = data.get("source_commit")
    if not isinstance(tag, str) or release_channel(tag) != channel:
        raise ReleaseManifestError("release pointer tag and channel disagree")
    if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseManifestError("release pointer source commit is invalid")
    if data.get("bundle_index_url") != bundle_url(commit, "bundle-index.json"):
        raise ReleaseManifestError("release pointer bundle URL is invalid")
    qualification = data.get("qualification")
    if not isinstance(qualification, dict) or not all(
        isinstance(qualification.get(key), str) and qualification[key].strip()
        for key in ("workflow_run", "qualified_at")
    ):
        raise ReleaseManifestError("release qualification is incomplete")
    return data


def canonical_pointer(data: Any) -> bytes:
    parse_release_pointer(data)
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def delete(url: str) -> None:
    try:
        with urllib.request.urlopen(
            request(url, method="DELETE"), timeout=HTTP_TIMEOUT_SECONDS,
        ):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise ReleaseManifestError(
                f"replacing release channel failed (HTTP {exc.code}): {url}"
            ) from exc
    except urllib.error.URLError as exc:
        raise ReleaseManifestError(
            f"release registry unreachable: {url} ({exc.reason})") from exc


def publish_qualification(
    pointer: dict[str, Any], *, allow_rollback: bool = False,
) -> tuple[str, str]:
    """Publish an immutable tag pointer and advance its channel."""
    parse_release_pointer(pointer)
    tag = str(pointer["tag"])
    channel = str(pointer["channel"])
    wanted = canonical_pointer(pointer)
    release_url = pointer_url(
        "bot-bottle-releases", tag, "release.json")
    channel_url = pointer_url(
        "bot-bottle-channels", channel, "channel.json")

    if remote_bytes(str(pointer["bundle_index_url"])) is None:
        raise ReleaseManifestError(
            f"commit bundle does not exist for {pointer['source_commit']}")
    existing_release = remote_bytes(release_url)
    if existing_release is not None and existing_release != wanted:
        raise ReleaseManifestError(
            f"immutable release pointer {tag} already differs")
    if existing_release is None:
        upload(release_url, wanted)

    existing_channel = remote_bytes(channel_url)
    if existing_channel == wanted:
        return release_url, channel_url
    if existing_channel is not None:
        try:
            current = parse_release_pointer(json.loads(existing_channel))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReleaseManifestError(
                f"existing {channel} channel pointer is malformed") from exc
        if (
            not allow_rollback
            and release_version(tag) <= release_version(str(current["tag"]))
        ):
            raise ReleaseManifestError(
                f"{channel} channel update is not monotonic; "
                "use the explicit rollback operation")
        delete(channel_url)
    upload(channel_url, wanted)
    return release_url, channel_url


__all__ = [
    "build_release_pointer",
    "canonical_pointer",
    "parse_release_pointer",
    "publish_qualification",
    "release_channel",
    "release_version",
]
