"""Resolve and verify published images against the registry.

Two steps: check the tag still resolves to the pinned digest (a digest pull
ignores the tag, so this is the only way a re-pointed tag surfaces), then pull
by digest. Both fail closed and never fall back to a local build.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import oci_registry
from .published_artifacts import Artifact, FORMAT_EXT4, FORMAT_OCI

REGISTRY_HTTP_TIMEOUT_SECONDS = 30.0
# These are hundreds of MB.
ARTIFACT_HTTP_TIMEOUT_SECONDS = 300.0
_CHUNK = 1 << 20

# OCI media types a manifest request must accept, or a registry may answer with
# a legacy schema or refuse an index.
_ACCEPT = ", ".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))

# One package-scoped token, shared with the generic-package artifacts. It goes
# only to the host named in TOKEN_HOST_ENV (unset: nowhere), so a token for a
# private registry never reaches a public one the pins also name.
TOKEN_ENV = "BOT_BOTTLE_INFRA_ARTIFACT_TOKEN"
TOKEN_HOST_ENV = "BOT_BOTTLE_INFRA_ARTIFACT_TOKEN_HOST"


def token_for(host: str) -> str:
    """The artifact token, if `host` is the one it is scoped to."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    scoped = os.environ.get(TOKEN_HOST_ENV, "").strip().lower()
    return token if token and scoped and host.lower() == scoped else ""


def _host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


class RegistryError(RuntimeError):
    """The registry was unreachable, answered unexpectedly, or disagreed with
    the manifest. Always fatal."""


class DigestMismatch(RegistryError):
    """The tag no longer resolves to the pinned digest. Investigated, not
    retried."""


@dataclass(frozen=True)
class ResolvedImage:
    """A verified reference, safe to pull."""

    artifact: Artifact
    # `repo@sha256:...`, what to pull. The tag is left out.
    pinned_ref: str


def _registry_host(ref: str) -> str:
    """The registry host of `host/owner/name`: the first segment, which carries
    a dot or a port."""
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
    token = token_for(_host_of(url))
    if token:
        # The OCI /v2/ API wants HTTP Basic (any username, the token as
        # password), not Gitea's REST `token <PAT>` scheme. `_download` keeps
        # the `token` scheme.
        creds = base64.b64encode(f"bot-bottle:{token}".encode()).decode()
        req.add_unredirected_header("Authorization", f"Basic {creds}")
    return req


def _head_digest(url: str, repo: str) -> str:
    """HEAD a manifest and return its `Docker-Content-Digest`. A 401 Bearer
    challenge gets one pull-scoped token request and a retry."""
    req = _request(url)
    req.get_method = lambda: "HEAD"  # type: ignore[method-assign]
    try:
        with urllib.request.urlopen(req, timeout=REGISTRY_HTTP_TIMEOUT_SECONDS) as resp:
            return resp.headers.get("Docker-Content-Digest", "").strip()
    except urllib.error.HTTPError as e:
        # `headers` is None on an HTTPError raised without any.
        get_all = getattr(e.headers, "get_all", None)
        challenge = _bearer_challenge((get_all("WWW-Authenticate") if get_all else None) or [])
        if e.code != 401 or challenge is None:
            raise
    retry = urllib.request.Request(url, method="HEAD", headers={
        "Accept": _ACCEPT, "Authorization": f"Bearer {_bearer_token(challenge, repo)}"})
    with urllib.request.urlopen(retry, timeout=REGISTRY_HTTP_TIMEOUT_SECONDS) as resp:
        return resp.headers.get("Docker-Content-Digest", "").strip()


def _bearer_challenge(headers: list[str]) -> dict[str, str] | None:
    """The parameters of a `Bearer realm=...,service=...` challenge, if any."""
    for header in headers:
        scheme, _, params = header.partition(" ")
        if scheme.lower() == "bearer":
            return {k.strip().lower(): v.strip().strip('"') for k, _, v in
                    (p.partition("=") for p in params.split(",")) if k.strip()}
    return None


def _bearer_token(challenge: dict[str, str], repo: str) -> str:
    """A pull token for `repo` from the challenge's realm, anonymous or with
    the artifact token as Basic credentials."""
    query = urllib.parse.urlencode({
        "service": challenge.get("service", ""), "scope": f"repository:{repo}:pull"})
    req = _request(f"{challenge['realm']}?{query}")
    req.remove_header("Accept")
    with urllib.request.urlopen(req, timeout=REGISTRY_HTTP_TIMEOUT_SECONDS) as resp:
        body = json.load(resp)
    token = body.get("token") or body.get("access_token")
    if not token:
        raise RegistryError(f"{challenge['realm']} returned no token")
    return str(token)


def resolve_tag_digest(ref: str, tag: str, *, scheme: str = "https") -> str:
    """What `tag` resolves to now, via one manifest HEAD."""
    host, repo = _registry_host(ref), _repository(ref)
    url = f"{scheme}://{host}/v2/{repo}/manifests/{tag}"
    try:
        digest = _head_digest(url, repo)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RegistryError(
                f"{ref}:{tag} is not published (HTTP 404 at {url})"
            ) from e
        raise RegistryError(f"resolving {ref}:{tag} failed (HTTP {e.code})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RegistryError(f"registry unreachable: {url} ({e})") from e
    except ValueError as e:  # a token endpoint that didn't answer JSON
        raise RegistryError(f"registry token request for {ref} failed ({e})") from e
    if not digest:
        raise RegistryError(
            f"registry did not return a Docker-Content-Digest for {ref}:{tag}; "
            f"cannot confirm the tag still points at the pinned digest"
        )
    return digest


def _split_oci_url(url: str) -> tuple[str, str]:
    """`host/owner/name:tag` -> (`host/owner/name`, `tag`). The tag follows the
    last colon, which a `:port` can't be. A missing tag raises."""
    ref, sep, tag = url.rpartition(":")
    if not sep or not tag or "/" in tag:
        raise RegistryError(
            f"oci url {url!r} has no tag; expected host/owner/name:tag"
        )
    return ref, tag


def _url_filename(url: str) -> str:
    """The last path segment of a download url."""
    return url.rsplit("/", 1)[-1]


def verify_tag(artifact: Artifact, *, scheme: str = "https") -> None:
    """Raise `DigestMismatch` if the artifact's tag no longer resolves to its
    pinned digest (a published version was overwritten)."""
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
    """The digest-pinned reference to pull."""
    ref, _ = _split_oci_url(artifact.url)
    return f"{ref}@{artifact.digest}"


def resolve(
    artifact: Artifact, *, verify: bool = True, scheme: str = "https",
) -> ResolvedImage:
    """Verify (unless `verify=False`, for offline use) and return the reference
    to pull. The pull stays digest-pinned either way."""
    if verify:
        verify_tag(artifact, scheme=scheme)
    return ResolvedImage(artifact=artifact, pinned_ref=pinned_reference(artifact))


def pull(
    resolved: ResolvedImage, *, runtime: str = "docker", argv: tuple[str, ...] = (),
) -> None:
    """Pull the verified reference with `runtime`, or with the command prefix
    `argv` for a CLI whose pull isn't `<runtime> pull`. Never falls back to a
    build."""
    cmd = [*(argv or (runtime, "pull")), resolved.pinned_ref]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, text=True,
                              timeout=PULL_TIMEOUT_SECONDS)
    except OSError as e:
        raise RegistryError(f"{runtime} not available to pull images: {e}") from e
    except subprocess.TimeoutExpired as e:
        # Apple's `container image pull` waits forever on a registry it can't
        # read.
        raise RegistryError(
            f"pulling {resolved.pinned_ref} did not finish in {PULL_TIMEOUT_SECONDS // 60} "
            f"minutes. {_ACCESS_HINT}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        hint = f"\n{_ACCESS_HINT}" if _looks_unauthorized(detail) else ""
        raise RegistryError(
            f"pulling {resolved.pinned_ref} failed (exit {proc.returncode}):\n{detail}{hint}"
        )


# Enough for a multi-GB agent image on a slow link.
PULL_TIMEOUT_SECONDS = 30 * 60
_ACCESS_HINT = (
    "If the registry isn't public, log the container runtime in to it "
    "(`docker login` / `container registry login`), or set "
    "BOT_BOTTLE_INFRA_BUILD=local to build instead.")


def _looks_unauthorized(detail: str) -> bool:
    lowered = detail.lower()
    return any(word in lowered for word in ("unauthorized", "denied", "401", "403"))


def ext4_url(artifact: Artifact) -> str:
    """Where an ext4 artifact lives, as the manifest has it: an HTTPS download
    (a generic package) or an OCI artifact reference (`host/owner/name:tag`)."""
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


def ensure_ext4(artifact: Artifact, *, cache_root: Path) -> Path:
    """The verified, cached `.gz` for an ext4 artifact, downloaded once.

    A `.verified` marker beside the file marks a complete, checked download. A
    digest mismatch deletes the file and raises."""
    if artifact.format != FORMAT_EXT4:
        raise RegistryError(
            f"ensure_ext4 is for {FORMAT_EXT4} artifacts, got {artifact.format!r}"
        )
    # Keyed on digest, since a version can be republished with different bytes.
    root = cache_root / artifact.image / artifact.digest.replace(":", "-")
    root.mkdir(parents=True, exist_ok=True)
    url = ext4_url(artifact)
    from_oci = "://" not in url
    gz = root / ("rootfs.ext4.gz" if from_oci else _url_filename(url))
    verified = root / ".verified"
    if gz.is_file() and verified.is_file():
        return gz

    if from_oci:
        _download_oci_blob(url, artifact.digest, gz)
    else:
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


def _download_oci_blob(ref: str, digest: str, dest: Path) -> None:
    """Fetch the rootfs layer of an OCI artifact by its pinned digest. The tag
    in `ref` only names it: the digest is what gets fetched and checked."""
    try:
        parsed = oci_registry.parse_ref(ref)
        registry = oci_registry.Registry(parsed.host, token=token_for(parsed.host))
        registry.download_blob(parsed.repo, digest, dest)
    except oci_registry.OciRegistryError as e:
        raise RegistryError(f"fetching the rootfs {ref}@{digest} failed: {e}") from e


def _download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` atomically via a `.part` sibling."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url)
    token = token_for(_host_of(url))
    if token:
        req.add_unredirected_header("Authorization", f"token {token}")
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


__all__ = [
    "DigestMismatch",
    "ensure_ext4",
    "ext4_url",
    "RegistryError",
    "ResolvedImage",
    "pinned_reference",
    "pull",
    "resolve",
    "resolve_tag_digest",
    "token_for",
    "verify_tag",
]
