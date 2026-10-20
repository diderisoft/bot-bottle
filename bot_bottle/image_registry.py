"""Resolve and verify published images against the registry.

`published_artifacts` says what a checkout expects; this fetches it and proves
the registry agrees.

Resolution is two steps. Ask the registry what the tag resolves to now and
compare against the pinned digest — a digest pull ignores the tag, so this is
the only way a re-pointed tag surfaces. Then pull by digest. Detection, then
integrity.

Both fail closed and never fall back to a local build.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .paths import host_artifact_cache_dir
from .published_artifacts import Artifact, FORMAT_EXT4, FORMAT_OCI

REGISTRY_HTTP_TIMEOUT_SECONDS = 30.0
# Generous by comparison: these are hundreds of MB, not a manifest HEAD.
ARTIFACT_HTTP_TIMEOUT_SECONDS = 300.0
_CHUNK = 1 << 20

# The OCI distribution media types a manifest request must accept. Without these
# a registry may answer with a legacy schema, or refuse an image index outright.
_ACCEPT = ", ".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))

# Shared with the generic-package artifacts: one package-scoped token, kept
# separate from the general-purpose Gitea token.
TOKEN_ENV = "BOT_BOTTLE_INFRA_ARTIFACT_TOKEN"


class RegistryError(RuntimeError):
    """The registry was unreachable, answered unexpectedly, or disagreed with
    the manifest. Always fatal to the caller — there is no safe degraded mode
    for "I could not confirm what I am about to run"."""


class DigestMismatch(RegistryError):
    """The tag no longer resolves to the pinned digest.

    Distinct from a transport failure because the remediation is completely
    different: a transport error is retried, this one is investigated."""


@dataclass(frozen=True)
class ResolvedImage:
    """A verified reference, safe to pull."""

    artifact: Artifact
    # `repo@sha256:…` — what to actually pull. The tag is deliberately absent:
    # it has served its purpose (detection) by this point, and including it in
    # the pull reference would invite the impression it is being trusted.
    pinned_ref: str


def _registry_host(ref: str) -> str:
    """The registry host from an image reference. A reference is
    `host/owner/name`, and the host is the first segment — distinguishable from
    a bare owner because it carries a dot or a port."""
    head = ref.split("/", 1)[0]
    if "." not in head and ":" not in head:
        raise RegistryError(
            f"image ref {ref!r} has no registry host; expected host/owner/name"
        )
    return head


def _repository(ref: str) -> str:
    """The `owner/name` path within the registry."""
    parts = ref.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        raise RegistryError(
            f"image ref {ref!r} has no repository path; expected host/owner/name"
        )
    return parts[1]


def _request(url: str) -> urllib.request.Request:
    req = urllib.request.Request(url)
    req.add_header("Accept", _ACCEPT)
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        # The OCI /v2/ distribution API (the only thing _request talks to)
        # rejects Gitea's REST `token <PAT>` scheme with 401; it wants HTTP
        # Basic, and Gitea accepts any username with the token as the password.
        # The generic-package download in _download keeps the `token` scheme.
        creds = base64.b64encode(f"bot-bottle:{token}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    return req


def resolve_tag_digest(ref: str, tag: str, *, scheme: str = "https") -> str:
    """Ask the registry what `tag` currently resolves to, without pulling.

    Uses a HEAD against the manifest endpoint: the digest comes back in
    `Docker-Content-Digest`, so this costs one round trip and no image bytes."""
    host, repo = _registry_host(ref), _repository(ref)
    url = f"{scheme}://{host}/v2/{repo}/manifests/{tag}"
    req = _request(url)
    req.get_method = lambda: "HEAD"  # type: ignore[method-assign]
    try:
        with urllib.request.urlopen(
            req, timeout=REGISTRY_HTTP_TIMEOUT_SECONDS,
        ) as resp:
            digest = resp.headers.get("Docker-Content-Digest", "").strip()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RegistryError(
                f"{ref}:{tag} is not published (HTTP 404 at {url})"
            ) from e
        raise RegistryError(f"resolving {ref}:{tag} failed (HTTP {e.code})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RegistryError(f"registry unreachable: {url} ({e})") from e
    if not digest:
        raise RegistryError(
            f"registry did not return a Docker-Content-Digest for {ref}:{tag}; "
            f"cannot confirm the tag still points at the pinned digest"
        )
    return digest


def _split_oci_url(url: str) -> tuple[str, str]:
    """`host/owner/name:tag` -> (`host/owner/name`, `tag`).

    The tag is whatever follows the last colon, which is unambiguous even when
    the host carries a `:port`: that colon precedes the first `/`, so it never
    wins the split. A url with no tag (or a `:` that is really a port) is a
    manifest bug, caught here rather than at pull time."""
    ref, sep, tag = url.rpartition(":")
    if not sep or not tag or "/" in tag:
        raise RegistryError(
            f"oci url {url!r} has no tag; expected host/owner/name:tag"
        )
    return ref, tag


def _url_filename(url: str) -> str:
    """The trailing path segment of a download url — the file to cache it as."""
    return url.rsplit("/", 1)[-1]


def verify_tag(artifact: Artifact, *, scheme: str = "https") -> None:
    """Confirm the artifact's tag still resolves to its pinned digest, raising
    `DigestMismatch` if it does not.

    A moved tag means a published version was overwritten; pinning alone would
    hide it."""
    if artifact.format != FORMAT_OCI:
        raise RegistryError(
            f"verify_tag is for {FORMAT_OCI} artifacts, got {artifact.format!r}"
        )
    ref, tag = _split_oci_url(artifact.url)
    actual = resolve_tag_digest(ref, tag, scheme=scheme)
    if actual != artifact.digest:
        raise DigestMismatch(
            f"{ref}:{tag} has been re-pointed.\n"
            f"    pinned in {artifact.image}: {artifact.digest}\n"
            f"    registry now returns:      {actual}\n"
            f"    refusing to continue — the published version was overwritten, "
            f"or the tag was tampered with."
        )


def pinned_reference(artifact: Artifact) -> str:
    """The digest-pinned reference to pull. The tag is not part of it."""
    ref, _ = _split_oci_url(artifact.url)
    return f"{ref}@{artifact.digest}"


def resolve(
    artifact: Artifact, *, verify: bool = True, scheme: str = "https",
) -> ResolvedImage:
    """Verify (unless skipped) and return the reference to pull.

    `verify=False` is for offline use, where the tag cannot be resolved: it
    gives up detection but the pull stays digest-pinned."""
    if verify:
        verify_tag(artifact, scheme=scheme)
    return ResolvedImage(artifact=artifact, pinned_ref=pinned_reference(artifact))


def pull(
    resolved: ResolvedImage, *, runtime: str = "docker", argv: tuple[str, ...] = (),
) -> None:
    """Pull the verified reference with `runtime` (or the explicit command
    prefix `argv`, for a CLI whose pull isn't `<runtime> pull`).

    Deliberately never falls back to building: this path exists precisely
    because the image should come from the registry, and a silent local build
    would produce bytes nobody verified. Callers wanting a build ask for one
    explicitly."""
    cmd = [*(argv or (runtime, "pull")), resolved.pinned_ref]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, text=True,
                              timeout=PULL_TIMEOUT_SECONDS)
    except OSError as e:
        raise RegistryError(f"{runtime} not available to pull images: {e}") from e
    except subprocess.TimeoutExpired as e:
        # Apple's `container image pull` waits forever on a registry it isn't
        # allowed to read, rather than failing.
        raise RegistryError(
            f"pulling {resolved.pinned_ref} did not finish in {PULL_TIMEOUT_SECONDS // 60} "
            f"minutes. {_ACCESS_HINT}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        hint = f"\n{_ACCESS_HINT}" if _looks_unauthorized(detail) else ""
        raise RegistryError(
            f"pulling {resolved.pinned_ref} failed (exit {proc.returncode}):\n{detail}{hint}"
        )


# Long enough for a multi-GB agent image on a slow link.
PULL_TIMEOUT_SECONDS = 30 * 60
_ACCESS_HINT = (
    "If the registry isn't public, log the container runtime in to it "
    "(`docker login` / `container registry login`), or set "
    "BOT_BOTTLE_INFRA_BUILD=local to build instead.")


def _looks_unauthorized(detail: str) -> bool:
    lowered = detail.lower()
    return any(word in lowered for word in ("unauthorized", "denied", "401", "403"))


def ext4_url(artifact: Artifact) -> str:
    """The generic-package URL for an ext4 artifact — baked into the manifest at
    publish time, so consuming it is just reading `url`."""
    if artifact.format != FORMAT_EXT4:
        raise RegistryError(
            f"ext4_url is for {FORMAT_EXT4} artifacts, got {artifact.format!r}"
        )
    return artifact.url


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_ext4(artifact: Artifact, *, cache_root: Path | None = None) -> Path:
    """The verified, cached `.gz` for an ext4 artifact, downloading it once.

    The counterpart to the OCI half: `pull` hands a digest to the container
    runtime and lets it fetch and verify, but a generic package has no runtime
    to delegate to, so the download, the digest check and the cache all live
    here.

    Cached because these are hundreds of MB and a host boots the same version
    repeatedly. A `.verified` marker beside the file is what makes the cache
    trustworthy — a `.gz` alone could be a partial download from a previous
    interrupted run.

    Fails closed on a mismatch, deleting the file: a rootfs that failed
    verification must not be left where the next run would take it for
    cached-and-checked."""
    if artifact.format != FORMAT_EXT4:
        raise RegistryError(
            f"ensure_ext4 is for {FORMAT_EXT4} artifacts, got {artifact.format!r}"
        )
    if cache_root is None:
        cache_root = host_artifact_cache_dir()

    # Content-addressed, by digest rather than by (image, version, platform).
    # A version can be republished with different bytes — `--force` exists — and
    # a path that does not include the digest would then serve the previous
    # download forever, because a cache hit returns before anything is compared
    # to the manifest. Keying on the digest makes a hit provably the right bytes.
    root = cache_root / artifact.image / artifact.digest.replace(":", "-")
    root.mkdir(parents=True, exist_ok=True)
    gz = root / _url_filename(artifact.url)
    verified = root / ".verified"
    if gz.is_file() and verified.is_file():
        return gz

    url = ext4_url(artifact)
    _download(url, gz)

    expected = artifact.digest.split(":", 1)[1]
    actual = _sha256_file(gz)
    if actual != expected:
        gz.unlink(missing_ok=True)
        raise RegistryError(
            f"{artifact.image} {artifact.version} ({artifact.platform}) failed "
            f"verification:\n    expected sha256:{expected}\n"
            f"    actual   sha256:{actual}\n"
            f"    refusing to use an unverified rootfs."
        )
    verified.write_text("ok\n")
    return gz


def _download(url: str, dest: Path) -> None:
    """Stream `url` to `dest`, atomically via a `.part` sibling so an
    interrupted download can never be mistaken for a complete one."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url)
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(
            req, timeout=ARTIFACT_HTTP_TIMEOUT_SECONDS,
        ) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, _CHUNK)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.code == 404:
            raise RegistryError(
                f"not published for this version:\n    missing: {url}\n"
                f"    publish it (python3 -m bot_bottle.publish_images), or "
                f"build locally with BOT_BOTTLE_INFRA_BUILD=local"
            ) from e
        raise RegistryError(f"downloading {url} failed (HTTP {e.code})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise RegistryError(f"registry unreachable: {url} ({e})") from e
    tmp.replace(dest)


def local_build_requested() -> bool:
    """True when the operator opted into building instead of pulling.

    A supported path, not a dev-only hatch: it is the answer for a current or
    custom agent harness. Same env var the firecracker artifact path honours."""
    return os.environ.get("BOT_BOTTLE_INFRA_BUILD", "").strip().lower() == "local"


__all__ = [
    "DigestMismatch",
    "ensure_ext4",
    "ext4_url",
    "RegistryError",
    "ResolvedImage",
    "local_build_requested",
    "pinned_reference",
    "pull",
    "resolve",
    "resolve_tag_digest",
    "verify_tag",
]
