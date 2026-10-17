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

Or, instead of those, a free-form `dockerfile`: a single-stage Dockerfile that
builds `FROM ${BASE}`, staged with its own directory as the build context. The
staged copy pins `BASE` to the verified base (`ARG BASE=<ref@digest>`, so no
build-arg plumbing is needed per backend) and restores the provider's
`image_user` at the end.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
from pathlib import Path

from .agent_provider import get_provider
from .manifest import ManifestError, ManifestImageOverlay
from .manifest.agent import HARNESS_VERSION_RE

# Where overlay `files` are placed in the image. A fixed, documented location
# rather than an arbitrary path: predictable for the agent to read and safe from
# clobbering system paths. A future `dest` field could override this.
OVERLAY_DEST = "/opt/bot-bottle/overlay"

# Name of the staged overlay Dockerfile inside the build context. Dotted so it
# is unlikely to clobber a file in a user's `dockerfile` context directory.
STAGED_DOCKERFILE = ".bb-overlay.Dockerfile"


class OverlayError(ManifestError):
    """An overlay cannot be rendered or staged (unsupported directive, a missing
    file, a Dockerfile that doesn't build FROM ${BASE}, or no published base).
    A ManifestError so the CLI reports it as a config problem, not a crash."""


def no_published_base_error(template: str) -> OverlayError:
    """The error for an overlay declared where there is no pinned base to build
    it on — before the first release publishes agent images, or when the
    published manifest has no entry for this template. Fail closed: building
    the default image instead would silently drop the declared overlay."""
    return OverlayError(
        f"agent_provider.overlay needs a published, digest-pinned "
        f"{template!r} agent image to build on, but none is pinned for this "
        "install (no published-artifacts.json, or no entry for this template). "
        "Remove the overlay, or use a release that publishes agent images."
    )


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


_FROM_BASE_RE = re.compile(r"\$(\{BASE\}|BASE)(\s+as\s+[A-Za-z0-9_.-]+)?", re.IGNORECASE)
_ARG_BASE_RE = re.compile(r"\s*ARG\s+BASE\s*$", re.IGNORECASE)
# BuildKit parser directives (`# syntax=...`, `# escape=...`) only count at the
# very top of the file, so they stay above the staged header.
_DIRECTIVE_RE = re.compile(r"#\s*[A-Za-z]+\s*=.*")


def _instructions(text: str) -> list[tuple[int, str, str]]:
    """Logical Dockerfile instructions as (first physical line index, KEYWORD,
    rest): comments and blank lines dropped, backslash continuations joined."""
    out: list[tuple[int, str, str]] = []
    buf: list[str] = []
    start = 0
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if not buf:
            start = i
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        joined = " ".join(buf).strip()
        buf = []
        keyword, _, rest = joined.partition(" ")
        out.append((start, keyword.upper(), rest.strip()))
    if buf:
        joined = " ".join(buf).strip()
        keyword, _, rest = joined.partition(" ")
        out.append((start, keyword.upper(), rest.strip()))
    return out


def render_overlay_dockerfile_text(base_ref: str, text: str, *, template: str) -> str:
    """Validate a user overlay Dockerfile and return the staged copy.

    The Dockerfile must be single-stage and build `FROM ${BASE}` (or `$BASE`),
    so the image is always a layer on the verified base: only `ARG`s may come
    before that one `FROM`, and no other `FROM` may follow. `BASE` is pinned by
    an `ARG BASE=<base_ref>` prepended to the staged copy; the user's own bare
    `ARG BASE` (before the FROM) is dropped, and giving `BASE` a value is an
    error, since that would replace the verified base. The provider's
    `image_user` is restored at the end, so a Dockerfile that switches to root
    can't leave the agent running as root."""
    instrs = _instructions(text)
    froms = [(i, rest) for i, kw, rest in instrs if kw == "FROM"]
    if len(froms) != 1:
        raise OverlayError(
            f"overlay Dockerfile must have exactly one FROM (found {len(froms)}); "
            "multi-stage builds are not supported in an overlay"
        )
    from_line, from_rest = froms[0]
    if not _FROM_BASE_RE.fullmatch(from_rest):
        raise OverlayError(
            f"overlay Dockerfile must build FROM ${{BASE}} (got FROM {from_rest}); "
            "bot-bottle sets BASE to the verified, digest-pinned agent image"
        )
    drop: set[int] = set()
    for i, kw, rest in instrs:
        if i >= from_line:
            break
        if kw != "ARG":
            raise OverlayError(
                f"overlay Dockerfile has {kw} before FROM; only ARG may precede it"
            )
        name = rest.split("=", 1)[0].strip()
        if name.upper() == "BASE":
            if "=" in rest:
                raise OverlayError(
                    "overlay Dockerfile must not give BASE a value; bot-bottle "
                    "sets it to the verified, digest-pinned agent image"
                )
            drop.add(i)
    lines = [
        ln for i, ln in enumerate(text.splitlines())
        if not (i in drop and _ARG_BASE_RE.fullmatch(ln))
    ]
    directives: list[str] = []
    while lines and _DIRECTIVE_RE.fullmatch(lines[0].strip()):
        directives.append(lines.pop(0))
    user = get_provider(template).image_user
    return (
        "".join(d + "\n" for d in directives)
        + "# Staged by bot-bottle (PRD 0085 overlay): BASE is the verified,\n"
        "# digest-pinned agent image; the provider's user is restored at the end.\n"
        f"ARG BASE={base_ref}\n"
        + "\n".join(lines).rstrip("\n")
        + f"\nUSER {user}\n"
    )


def _harness_lines(template: str, version: str) -> tuple[str, ...]:
    # Re-checked here, not only at manifest parse: the version lands in RUN.
    if not HARNESS_VERSION_RE.fullmatch(version):
        raise OverlayError(f"invalid harness_version {version!r}")
    lines = get_provider(template).harness_overlay_lines(version)
    if lines is None:
        raise OverlayError(
            f"agent provider {template!r} does not support "
            "agent_provider.overlay.harness_version; "
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
    if overlay.dockerfile:
        src = source_root / overlay.dockerfile
        if not src.is_file():
            raise OverlayError(
                f"overlay dockerfile not found: {overlay.dockerfile!r} "
                f"(looked in {source_root})"
            )
        staged = render_overlay_dockerfile_text(
            base_ref, src.read_text(), template=template)
        # The Dockerfile's own directory is the build context, so its COPYs
        # resolve next to it (the custom-dockerfile build-context gap, closed
        # for overlays).
        shutil.copytree(src.parent, dest, dirs_exist_ok=True)
        dockerfile = dest / STAGED_DOCKERFILE
        dockerfile.write_text(staged)
        return dockerfile
    dockerfile = dest / STAGED_DOCKERFILE
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


def overlay_context_digest(overlay: ManifestImageOverlay, source_root: Path) -> str:
    """A digest of the bytes the overlay build reads from the host: each
    `files` entry, or, for a `dockerfile` overlay, every file in the
    Dockerfile's directory (its build context). Folded into the cache tag so an
    edit to a file's contents rebuilds, not just a change to the spec. Missing
    inputs fail loudly, like staging."""
    h = hashlib.sha256()
    if overlay.dockerfile:
        src = source_root / overlay.dockerfile
        if not src.is_file():
            raise OverlayError(
                f"overlay dockerfile not found: {overlay.dockerfile!r} "
                f"(looked in {source_root})"
            )
        ctx = src.parent
        paths: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(ctx):
            dirnames.sort()
            paths.extend(Path(dirpath) / f for f in sorted(filenames))
        for p in paths:
            h.update(p.relative_to(ctx).as_posix().encode() + b"\0")
            h.update(p.read_bytes() + b"\0")
        return h.hexdigest()
    for rel in overlay.files:
        src = source_root / rel
        if not src.is_file():
            raise OverlayError(
                f"overlay file not found: {rel!r} (looked in {source_root})"
            )
        h.update(rel.encode() + b"\0")
        h.update(src.read_bytes() + b"\0")
    return h.hexdigest()


def overlay_cache_tag(
    base_digest: str, overlay: ManifestImageOverlay, *, context_digest: str = "",
) -> str:
    """A content-addressed tag suffix for an overlaid image: the base digest
    plus the overlay spec (and, via `context_digest`, the contents of the host
    files it reads), so a change to any of them rebuilds the layer and neither
    a base bump nor an overlay edit reuses a stale one."""
    h = hashlib.sha256()
    h.update(base_digest.encode())
    h.update(b"\0harness\0")
    h.update(overlay.harness_version.encode())
    h.update(b"\0apt\0")
    h.update("\0".join(overlay.apt).encode())
    h.update(b"\0files\0")
    h.update("\0".join(overlay.files).encode())
    h.update(b"\0dockerfile\0")
    h.update(overlay.dockerfile.encode())
    h.update(b"\0context\0")
    h.update(context_digest.encode())
    return h.hexdigest()[:16]


__all__ = [
    "OVERLAY_DEST",
    "STAGED_DOCKERFILE",
    "OverlayError",
    "no_published_base_error",
    "render_overlay_dockerfile",
    "render_overlay_dockerfile_text",
    "stage_overlay_context",
    "overlay_context_digest",
    "overlay_cache_tag",
]
