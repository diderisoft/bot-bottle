"""Resolve and verify published images against the registry.

The manifest (`published_artifacts`) says what a checkout expects; this fetches it and
proves the registry agrees.

Pinning by digest already guarantees the *bytes* — a pull of
``name:tag@sha256:…`` resolves by digest and a runtime that returns anything
else is broken. What pinning does **not** tell you is that the tag still points
where it did, because the tag is not consulted during that pull at all. A
re-pointed tag is either an accidental overwrite or a tamper signal, and either
way it should surface rather than pass silently.

So resolution here is deliberately two steps: ask the registry what the tag
resolves to *now*, compare that against the pinned digest, and only then pull by
digest. The first step buys detection; the second buys integrity. Both fail
closed — never falling back to a local build, because silently building what you
failed to verify is how an unverified image gets run.
"""

from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from .published_artifacts import Artifact, FORMAT_OCI

REGISTRY_HTTP_TIMEOUT_SECONDS = 30.0

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
        req.add_header("Authorization", f"token {token}")
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


def verify_tag(artifact: Artifact, *, scheme: str = "https") -> None:
    """Confirm the artifact's tag still resolves to its pinned digest.

    Raises `DigestMismatch` when it does not. This is the check that pinning
    alone cannot perform, and the reason it is worth a round trip: a tag that
    has moved means someone overwrote a published version, and continuing would
    hide that."""
    if artifact.format != FORMAT_OCI:
        raise RegistryError(
            f"verify_tag is for {FORMAT_OCI} artifacts, got {artifact.format!r}"
        )
    actual = resolve_tag_digest(artifact.namespace, artifact.locator, scheme=scheme)
    if actual != artifact.digest:
        raise DigestMismatch(
            f"{artifact.namespace}:{artifact.locator} has been re-pointed.\n"
            f"    pinned in {artifact.image}: {artifact.digest}\n"
            f"    registry now returns:      {actual}\n"
            f"    refusing to continue — the published version was overwritten, "
            f"or the tag was tampered with."
        )


def pinned_reference(artifact: Artifact) -> str:
    """The digest-pinned reference to pull. The tag is not part of it."""
    return f"{artifact.namespace}@{artifact.digest}"


def resolve(
    artifact: Artifact, *, verify: bool = True, scheme: str = "https",
) -> ResolvedImage:
    """Verify (unless explicitly skipped) and return the reference to pull.

    `verify=False` exists for offline and air-gapped use, where the tag cannot
    be resolved at all. It gives up *detection* while keeping *integrity* — the
    pull is still digest-pinned — so it degrades honestly rather than silently."""
    if verify:
        verify_tag(artifact, scheme=scheme)
    return ResolvedImage(artifact=artifact, pinned_ref=pinned_reference(artifact))


def pull(resolved: ResolvedImage, *, runtime: str = "docker") -> None:
    """Pull the verified reference with `runtime`.

    Deliberately never falls back to building: this path exists precisely
    because the image should come from the registry, and a silent local build
    would produce bytes nobody verified. Callers wanting a build ask for one
    explicitly."""
    argv = [runtime, "pull", resolved.pinned_ref]
    try:
        proc = subprocess.run(argv, capture_output=True, check=False, text=True)
    except OSError as e:
        raise RegistryError(f"{runtime} not available to pull images: {e}") from e
    if proc.returncode != 0:
        raise RegistryError(
            f"pulling {resolved.pinned_ref} failed (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout).strip()}"
        )


def local_build_requested() -> bool:
    """True when the operator opted into building instead of pulling.

    Building stays a first-class, supported path — it is the answer for a
    current or custom agent harness, since published agent images are
    convenience artifacts that may lag upstream — not merely a dev escape
    hatch. Same environment variable the firecracker artifact path already
    honours, so one opt-in covers both."""
    return os.environ.get("BOT_BOTTLE_INFRA_BUILD", "").strip().lower() == "local"


__all__ = [
    "DigestMismatch",
    "RegistryError",
    "ResolvedImage",
    "local_build_requested",
    "pinned_reference",
    "pull",
    "resolve",
    "resolve_tag_digest",
    "verify_tag",
]
