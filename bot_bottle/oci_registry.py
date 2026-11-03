"""A small OCI distribution (`/v2/`) client, stdlib only: blobs and manifests,
for publishing the ext4 rootfs as an OCI artifact and copying images between
registries byte for byte.

Auth follows the registry's `Bearer` challenge: a 401 names a token realm,
which is asked for a token scoped to the repository, anonymously or with
`user`/`token` as HTTP Basic. Credentials never follow a redirect, since blob
downloads redirect to a CDN with a signed URL of its own.

Takes credentials as arguments and reads no environment.
"""

from __future__ import annotations

import hashlib
import base64
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

HTTP_TIMEOUT_SECONDS = 60.0
# Hundreds of MB each way; the registry hashes an upload before answering.
TRANSFER_TIMEOUT_SECONDS = 900.0
_CHUNK = 1 << 20

MANIFEST_OCI = "application/vnd.oci.image.manifest.v1+json"
INDEX_OCI = "application/vnd.oci.image.index.v1+json"
MANIFEST_DOCKER = "application/vnd.docker.distribution.manifest.v2+json"
LIST_DOCKER = "application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_TYPES = (MANIFEST_OCI, INDEX_OCI, MANIFEST_DOCKER, LIST_DOCKER)
_INDEX_TYPES = (INDEX_OCI, LIST_DOCKER)

# The rootfs as an OCI artifact: one gzip layer, the empty config.
ROOTFS_ARTIFACT_TYPE = "application/vnd.bot-bottle.rootfs.ext4+gzip"
EMPTY_CONFIG_TYPE = "application/vnd.oci.empty.v1+json"
EMPTY_CONFIG = b"{}"


class OciRegistryError(RuntimeError):
    """The registry refused, was unreachable, or answered unexpectedly."""


@dataclass(frozen=True)
class OciRef:
    """`host/owner/name[:tag]` split into its parts."""

    host: str
    repo: str
    tag: str = ""

    @property
    def name(self) -> str:
        return f"{self.host}/{self.repo}"


def parse_ref(ref: str) -> OciRef:
    """`ghcr.io/acme/app:1.0-amd64` -> OciRef. The host is the first segment and
    must carry a dot or a port; the tag follows the last colon after it."""
    if "://" in ref:
        raise OciRegistryError(f"{ref!r} is a URL, not an image reference")
    host, sep, rest = ref.partition("/")
    if not sep or not rest or ("." not in host and ":" not in host):
        raise OciRegistryError(f"{ref!r} is not host/owner/name[:tag]")
    repo, colon, tag = rest.rpartition(":")
    if not colon or "/" in tag:
        repo, tag = rest, ""
    if not repo or "@" in repo:
        raise OciRegistryError(f"{ref!r} is not host/owner/name[:tag]")
    return OciRef(host=host, repo=repo, tag=tag)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _digest_bytes(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _challenge(err: urllib.error.HTTPError) -> dict[str, str] | None:
    """The parameters of a `Bearer realm=...` challenge, if the 401 has one."""
    get_all = getattr(err.headers, "get_all", None)
    for header in (get_all("WWW-Authenticate") if get_all else None) or []:
        scheme, _, params = header.partition(" ")
        if scheme.lower() == "bearer":
            return {k.strip().lower(): v.strip().strip('"') for k, _, v in
                    (p.partition("=") for p in params.split(",")) if k.strip()}
    return None


@dataclass
class Registry:
    """One registry host, with optional credentials for its token realm.
    `scheme` is `http` only for a local test registry."""

    host: str
    user: str = ""
    token: str = ""
    scheme: str = "https"
    _tokens: dict[str, str] = field(default_factory=dict, repr=False)

    def _url(self, repo: str, path: str) -> str:
        return f"{self.scheme}://{self.host}/v2/{repo}/{path}"

    def _bearer(self, challenge: dict[str, str], repo: str, actions: str) -> str:
        scope = f"repository:{repo}:{actions}"
        if scope in self._tokens:
            return self._tokens[scope]
        query = urllib.parse.urlencode(
            {"service": challenge.get("service", ""), "scope": scope})
        req = urllib.request.Request(f"{challenge['realm']}?{query}")
        if self.token:
            creds = base64.b64encode(f"{self.user or 'bot-bottle'}:{self.token}".encode())
            req.add_unredirected_header("Authorization", f"Basic {creds.decode()}")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            raise OciRegistryError(
                f"{self.host}: token for {scope} refused (HTTP {e.code})") from e
        except ValueError as e:
            raise OciRegistryError(f"{self.host}: token realm answered no JSON") from e
        token = body.get("token") or body.get("access_token")
        if not token:
            raise OciRegistryError(f"{self.host}: token realm returned no token")
        self._tokens[scope] = str(token)
        return self._tokens[scope]

    def _open(self, method: str, url: str, repo: str, *, actions: str = "pull",
              headers: dict[str, str] | None = None, data: bytes | BinaryIO | None = None,
              timeout: float = HTTP_TIMEOUT_SECONDS):
        """Send once; on a Bearer challenge, fetch a scoped token and resend.
        A streamed body can't be resent, so a file upload relies on the token
        an earlier request in the same scope cached (`upload_blob`'s HEAD)."""
        def build(auth: str | None) -> urllib.request.Request:
            req = urllib.request.Request(url, data=data, method=method)  # type: ignore[arg-type]
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            if auth:
                req.add_unredirected_header("Authorization", auth)
            return req

        scope = f"repository:{repo}:{actions}"
        cached = self._tokens.get(scope)
        try:
            return urllib.request.urlopen(
                build(f"Bearer {cached}" if cached else None), timeout=timeout)
        except urllib.error.HTTPError as e:
            challenge = _challenge(e) if e.code == 401 else None
            if challenge is None or hasattr(data, "read"):
                raise
            # An expired token gets one fresh one.
            self._tokens.pop(scope, None)
        bearer = self._bearer(challenge, repo, actions)
        return urllib.request.urlopen(build(f"Bearer {bearer}"), timeout=timeout)

    # --- reads ---

    def manifest(self, repo: str, reference: str) -> tuple[bytes, str]:
        """A manifest's exact bytes and media type."""
        try:
            with self._open("GET", self._url(repo, f"manifests/{reference}"), repo,
                            headers={"Accept": ", ".join(MANIFEST_TYPES)}) as resp:
                body = resp.read()
                media = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        except urllib.error.HTTPError as e:
            raise OciRegistryError(
                f"{self.host}/{repo}:{reference}: manifest fetch failed (HTTP {e.code})") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OciRegistryError(f"{self.host} unreachable ({e})") from e
        return body, media or json.loads(body).get("mediaType", "")

    def manifest_exists(self, repo: str, reference: str) -> bool:
        try:
            with self._open("HEAD", self._url(repo, f"manifests/{reference}"), repo,
                            headers={"Accept": ", ".join(MANIFEST_TYPES)}):
                return True
        except urllib.error.HTTPError as e:
            # GHCR answers 401/403 for a package that doesn't exist yet.
            if e.code in (404, 401, 403):
                return False
            raise OciRegistryError(
                f"{self.host}/{repo}:{reference}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OciRegistryError(f"{self.host} unreachable ({e})") from e

    def blob_exists(self, repo: str, digest: str, *, actions: str = "pull") -> bool:
        try:
            with self._open("HEAD", self._url(repo, f"blobs/{digest}"), repo, actions=actions):
                return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 401, 403):
                return False
            raise OciRegistryError(f"{self.host}/{repo}@{digest}: HTTP {e.code}") from e

    def download_blob(self, repo: str, digest: str, dest: Path) -> None:
        """Stream a blob to `dest` atomically and check its digest."""
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._open("GET", self._url(repo, f"blobs/{digest}"), repo,
                            timeout=TRANSFER_TIMEOUT_SECONDS) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out, _CHUNK)
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise OciRegistryError(
                f"{self.host}/{repo}@{digest}: download failed (HTTP {e.code})") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            tmp.unlink(missing_ok=True)
            raise OciRegistryError(f"{self.host} unreachable ({e})") from e
        actual = sha256_file(tmp)
        if actual != digest:
            tmp.unlink(missing_ok=True)
            raise OciRegistryError(
                f"{self.host}/{repo}@{digest}: downloaded bytes hash to {actual}")
        tmp.replace(dest)

    # --- writes ---

    def upload_blob(self, repo: str, digest: str, body: Path | bytes) -> None:
        """Upload one blob monolithically, unless the repository has it."""
        if self.blob_exists(repo, digest, actions="pull,push"):
            return
        try:
            with self._open("POST", self._url(repo, "blobs/uploads/"), repo,
                            actions="pull,push", data=b"",
                            headers={"Content-Length": "0"}) as resp:
                location = resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            raise OciRegistryError(
                f"{self.host}/{repo}: upload refused (HTTP {e.code})") from e
        if not location:
            raise OciRegistryError(f"{self.host}/{repo}: upload gave no Location")
        target = urllib.parse.urljoin(f"{self.scheme}://{self.host}/", location)
        target += ("&" if "?" in target else "?") + urllib.parse.urlencode({"digest": digest})
        size = len(body) if isinstance(body, bytes) else body.stat().st_size
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        try:
            if isinstance(body, bytes):
                ctx = self._open("PUT", target, repo, actions="pull,push", data=body,
                                 headers=headers, timeout=TRANSFER_TIMEOUT_SECONDS)
                with ctx:
                    pass
            else:
                with open(body, "rb") as fh, self._open(
                        "PUT", target, repo, actions="pull,push", data=fh,
                        headers=headers, timeout=TRANSFER_TIMEOUT_SECONDS):
                    pass
        except urllib.error.HTTPError as e:
            raise OciRegistryError(
                f"{self.host}/{repo}@{digest}: upload failed (HTTP {e.code})") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OciRegistryError(f"{self.host}: upload of {digest} failed ({e})") from e

    def put_manifest(self, repo: str, reference: str, body: bytes, media_type: str) -> str:
        """PUT exact manifest bytes under `reference` (a tag or the digest) and
        return their digest."""
        try:
            with self._open("PUT", self._url(repo, f"manifests/{reference}"), repo,
                            actions="pull,push", data=body,
                            headers={"Content-Type": media_type}):
                pass
        except urllib.error.HTTPError as e:
            raise OciRegistryError(
                f"{self.host}/{repo}:{reference}: manifest refused (HTTP {e.code})") from e
        return _digest_bytes(body)


def rootfs_manifest(layer_digest: str, layer_size: int, *, title: str,
                    annotations: dict[str, str]) -> bytes:
    """The OCI artifact manifest for one gzipped rootfs."""
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST_OCI,
        "artifactType": ROOTFS_ARTIFACT_TYPE,
        "config": {"mediaType": EMPTY_CONFIG_TYPE,
                   "digest": _digest_bytes(EMPTY_CONFIG), "size": len(EMPTY_CONFIG)},
        "layers": [{"mediaType": ROOTFS_ARTIFACT_TYPE, "digest": layer_digest,
                    "size": layer_size,
                    "annotations": {"org.opencontainers.image.title": title}}],
        "annotations": annotations,
    }
    return json.dumps(manifest, indent=2, sort_keys=True).encode()


def push_rootfs(registry: Registry, repo: str, tag: str, gz: Path, *,
                annotations: dict[str, str]) -> str:
    """Publish a gzipped rootfs as `repo:tag` and return the layer's digest,
    which is what a manifest pins (the file's sha256)."""
    digest = sha256_file(gz)
    registry.upload_blob(repo, digest, gz)
    registry.upload_blob(repo, _digest_bytes(EMPTY_CONFIG), EMPTY_CONFIG)
    body = rootfs_manifest(digest, gz.stat().st_size, title=gz.name, annotations=annotations)
    registry.put_manifest(repo, tag, body, MANIFEST_OCI)
    return digest


def copy_manifest(src: Registry, src_repo: str, reference: str, *,
                  dst: Registry, dst_repo: str, tag: str, scratch: Path) -> str:
    """Copy a manifest (an index with its children, or an image with its
    blobs) byte for byte and tag it; returns its digest, which a copy can't
    change."""
    body, media = src.manifest(src_repo, reference)
    doc = json.loads(body)
    if media in _INDEX_TYPES or "manifests" in doc:
        for child in doc.get("manifests", []):
            copy_manifest(src, src_repo, child["digest"], dst=dst, dst_repo=dst_repo,
                          tag=child["digest"], scratch=scratch)
    else:
        for blob in [doc["config"], *doc.get("layers", [])]:
            digest = blob["digest"]
            if dst.blob_exists(dst_repo, digest, actions="pull,push"):
                continue
            part = scratch / digest.replace(":", "-")
            src.download_blob(src_repo, digest, part)
            try:
                dst.upload_blob(dst_repo, digest, part)
            finally:
                part.unlink(missing_ok=True)
    return dst.put_manifest(dst_repo, tag, body, media)


__all__ = [
    "OciRef",
    "OciRegistryError",
    "ROOTFS_ARTIFACT_TYPE",
    "Registry",
    "copy_manifest",
    "parse_ref",
    "push_rootfs",
    "rootfs_manifest",
    "sha256_file",
]
