"""Synthesize a thin overlay layer on a pinned agent image (PRD 0085, tier 2).

The overlay is the middle path between "pull the default as-is" and "bring your
own image": a one-layer `FROM <pinned-default>` build declared in agent
frontmatter, so the base stays the verified release image and compatibility is
inherited. This module renders the overlay Dockerfile and its content-addressed
cache tag; the per-backend build machinery does the actual build.

Supported directives: `apt` packages, a `harness_version` bump (rendered by the
provider's `AgentProvider.harness_overlay_lines`, since each harness installs
differently), and extra `files`, which land under ``OVERLAY_DEST`` in the image
preserving their relative path.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from pathlib import Path

from .agent_provider import get_provider
from .manifest import ManifestImageOverlay
from .manifest.agent import HARNESS_VERSION_RE

# Where overlay `files` are placed in the image. A fixed, documented location
# rather than an arbitrary path: predictable for the agent to read and safe from
# clobbering system paths. A future `dest` field could override this.
OVERLAY_DEST = "/opt/bot-bottle/overlay"


class OverlayError(RuntimeError):
    """An overlay cannot be rendered (unsupported directive)."""


def render_overlay_dockerfile(
    base_ref: str, overlay: ManifestImageOverlay, *, template: str,
) -> str:
    """The thin Dockerfile that layers `overlay` on `base_ref`.

    `base_ref` must be the digest-pinned reference to the verified default image,
    so the overlay inherits exactly those bytes as its base. `template` names the
    agent provider whose image this is: the default images end as a non-root
    user, so RUN steps switch to root and the provider's `image_user` is restored
    afterwards."""
    lines = [f"FROM {base_ref}"]
    runs: list[str] = []
    if overlay.apt:
        pkgs = " ".join(shlex.quote(p) for p in overlay.apt)
        # The default images point apt at snapshot.debian.org, whose Release
        # files are past their Valid-Until; match the base images' flag.
        runs.append(
            "RUN apt-get -o Acquire::Check-Valid-Until=false update "
            f"&& apt-get install -y --no-install-recommends {pkgs} "
            "&& rm -rf /var/lib/apt/lists/*"
        )
    if overlay.harness_version:
        runs.extend(_harness_lines(template, overlay.harness_version))
    if runs:
        lines.append("USER root")
        lines.extend(runs)
    for rel in overlay.files:
        dest = f"{OVERLAY_DEST}/{rel}"
        lines.append(f"COPY {shlex.quote(rel)} {shlex.quote(dest)}")
    if runs:
        lines.append(f"USER {get_provider(template).image_user}")
    return "\n".join(lines) + "\n"


def _harness_lines(template: str, version: str) -> tuple[str, ...]:
    # Re-checked here, not only at manifest parse: the version lands in RUN.
    if not HARNESS_VERSION_RE.fullmatch(version):
        raise OverlayError(f"invalid harness_version {version!r}")
    lines = get_provider(template).harness_overlay_lines(version)
    if lines is None:
        raise OverlayError(
            f"agent provider {template!r} does not support "
            "agent_provider.image.overlay.harness_version; "
            "pin the harness in a custom agent image instead"
        )
    return lines


def stage_overlay_context(
    base_ref: str,
    overlay: ManifestImageOverlay,
    *,
    template: str,
    source_root: Path,
    dest: Path,
) -> Path:
    """Write the overlay Dockerfile and copy its files into `dest`, returning the
    Dockerfile path. `source_root` is where the overlay's `files` resolve from —
    the launch cwd, matching how a custom `agent_provider.dockerfile` resolves.
    Missing files fail loudly rather than producing an image that silently omits
    them."""
    dest.mkdir(parents=True, exist_ok=True)
    dockerfile = dest / "Dockerfile.overlay"
    dockerfile.write_text(render_overlay_dockerfile(base_ref, overlay, template=template))
    for rel in overlay.files:
        src = source_root / rel
        if not src.is_file():
            raise OverlayError(
                f"overlay file not found: {rel!r} (looked in {source_root})"
            )
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    return dockerfile


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
    "stage_overlay_context",
    "overlay_cache_tag",
]
