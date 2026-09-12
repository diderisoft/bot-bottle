"""Synthesize a thin overlay layer on a pinned agent image (PRD 0085, tier 2).

The overlay is the middle path between "pull the default as-is" and "bring your
own image": a one-layer `FROM <pinned-default>` build declared in agent
frontmatter, so the base stays the verified release image and compatibility is
inherited. This module renders the overlay Dockerfile and its content-addressed
cache tag; the per-backend build machinery does the actual build.

Scope note (PRD open questions): `apt` packages and extra `files` are supported.
`harness_version` is not yet — bumping a provider's pinned harness needs a
per-provider "harness package" interface the PRD leaves unsettled, so it raises
rather than silently ignoring the request. Overlay `files` land under
``OVERLAY_DEST`` in the image, preserving their relative path.
"""

from __future__ import annotations

import hashlib
import shlex

from .manifest import ManifestImageOverlay

# Where overlay `files` are placed in the image. A fixed, documented location
# rather than an arbitrary path: predictable for the agent to read and safe from
# clobbering system paths. A future `dest` field could override this.
OVERLAY_DEST = "/opt/bot-bottle/overlay"


class OverlayError(RuntimeError):
    """An overlay cannot be rendered (unsupported directive)."""


def render_overlay_dockerfile(base_ref: str, overlay: ManifestImageOverlay) -> str:
    """The thin Dockerfile that layers `overlay` on `base_ref`.

    `base_ref` must be the digest-pinned reference to the verified default image,
    so the overlay inherits exactly those bytes as its base."""
    if overlay.harness_version:
        raise OverlayError(
            "agent_provider.image.overlay.harness_version is not yet supported; "
            "use apt/files, or pin the harness in a custom agent image"
        )
    lines = [f"FROM {base_ref}"]
    if overlay.apt:
        pkgs = " ".join(shlex.quote(p) for p in overlay.apt)
        lines.append(
            "RUN apt-get update "
            f"&& apt-get install -y --no-install-recommends {pkgs} "
            "&& rm -rf /var/lib/apt/lists/*"
        )
    for rel in overlay.files:
        dest = f"{OVERLAY_DEST}/{rel}"
        lines.append(f"COPY {shlex.quote(rel)} {shlex.quote(dest)}")
    return "\n".join(lines) + "\n"


def overlay_cache_tag(base_digest: str, overlay: ManifestImageOverlay) -> str:
    """A content-addressed tag suffix for an overlaid image: the base digest
    plus the overlay spec, so a change to either rebuilds the layer and neither
    a base bump nor an overlay edit reuses a stale one."""
    h = hashlib.sha256()
    h.update(base_digest.encode())
    h.update(b"\0harness\0")
    h.update(overlay.harness_version.encode())
    h.update(b"\0apt\0")
    h.update("\0".join(overlay.apt).encode())
    h.update(b"\0files\0")
    h.update("\0".join(overlay.files).encode())
    return h.hexdigest()[:16]


__all__ = [
    "OVERLAY_DEST",
    "OverlayError",
    "render_overlay_dockerfile",
    "overlay_cache_tag",
]
