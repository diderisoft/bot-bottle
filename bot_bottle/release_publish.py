"""Durable publication of immutable, commit-addressed release bundles."""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from pathlib import Path

from .release_bundle import canonical_bytes, parse_bundle_index, sha256_file
from .release_manifest import ReleaseManifestError

HTTP_TIMEOUT_SECONDS = 60.0
INDEX_NAME = "bundle-index.json"
PACKAGE_NAME = "bot-bottle-builds"


def config() -> tuple[str, str, str]:
    """Return generic-package base URL, owner, and write token."""
    base = os.environ.get(
        "BOT_BOTTLE_RELEASE_BASE", "https://gitea.dideric.is").rstrip("/")
    owner = os.environ.get("BOT_BOTTLE_RELEASE_OWNER", "didericis").strip()
    token = os.environ.get("BOT_BOTTLE_RELEASE_TOKEN", "")
    return base, owner, token


def bundle_url(source_commit: str, filename: str) -> str:
    base, owner, _ = config()
    return (
        f"{base}/api/packages/{owner}/generic/{PACKAGE_NAME}/"
        f"{source_commit}/{filename}"
    )


def request(url: str, *, method: str = "GET", data: object | None = None,
            length: int | None = None) -> urllib.request.Request:
    result = urllib.request.Request(url, data=data, method=method)  # type: ignore[arg-type]
    _, _, token = config()
    if token:
        result.add_header("Authorization", f"token {token}")
    if length is not None:
        result.add_header("Content-Length", str(length))
    result.add_header("Content-Type", "application/octet-stream")
    return result


def remote_bytes(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(
            request(url), timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseManifestError(
            f"checking release bundle failed (HTTP {exc.code}): {url}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseManifestError(
            f"release registry unreachable: {url} ({exc.reason})") from exc


def remote_sha256(url: str) -> str | None:
    try:
        with urllib.request.urlopen(
            request(url), timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:
            digest = hashlib.sha256()
            for chunk in iter(lambda: response.read(1 << 20), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseManifestError(
            f"checking release artifact failed (HTTP {exc.code}): {url}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseManifestError(
            f"release registry unreachable: {url} ({exc.reason})") from exc


def upload(url: str, source: bytes | Path) -> None:
    handle = None
    if isinstance(source, Path):
        handle = source.open("rb")
        data: object = handle
        length = source.stat().st_size
    else:
        data = source
        length = len(source)
    try:
        with urllib.request.urlopen(
            request(url, method="PUT", data=data, length=length),
            timeout=HTTP_TIMEOUT_SECONDS,
        ):
            pass
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ReleaseManifestError(
            f"publishing release artifact failed: {url} ({exc})") from exc
    finally:
        if handle is not None:
            handle.close()


def publish_bundle(index: dict[str, object], wheel: Path) -> str:
    """Publish wheel first and index last, or verify an existing exact bundle."""
    parse_bundle_index(index)
    source_commit = str(index["source_commit"])
    wheel_data = index["wheel"]
    assert isinstance(wheel_data, dict)
    filename = str(wheel_data["filename"])
    expected_sha = str(wheel_data["sha256"])
    if wheel.name != filename or sha256_file(wheel) != expected_sha:
        raise ReleaseManifestError(
            "release bundle wheel does not match its index")

    wheel_url = bundle_url(source_commit, filename)
    index_url = bundle_url(source_commit, INDEX_NAME)
    if wheel_data["url"] != wheel_url:
        raise ReleaseManifestError(
            "release bundle wheel URL does not match its commit coordinate")

    wanted_index = canonical_bytes(index)
    existing_index = remote_bytes(index_url)
    existing_wheel_sha = remote_sha256(wheel_url)
    if existing_index is not None:
        if existing_index != wanted_index or existing_wheel_sha != expected_sha:
            raise ReleaseManifestError(
                f"immutable release bundle {source_commit} already differs")
        return index_url
    if existing_wheel_sha is not None:
        raise ReleaseManifestError(
            f"partial release bundle {source_commit} exists without its index; "
            "administrative cleanup is required")

    upload(wheel_url, wheel)
    upload(index_url, wanted_index)
    return index_url


__all__ = [
    "INDEX_NAME",
    "PACKAGE_NAME",
    "bundle_url",
    "config",
    "publish_bundle",
]
