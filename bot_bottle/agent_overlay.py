"""Synthesize a thin overlay layer on a pinned agent image.

The overlay is the middle path between "pull the default as-is" and "bring your
own image": a one-layer `FROM <pinned-default>` build declared in agent
frontmatter, so the base stays the verified release image and compatibility is
inherited. This module renders and validates the overlay Dockerfile and stages
its build context; the per-backend build machinery does the actual build.

Supported directives: `apt` packages, a `harness_version` bump (rendered by the
provider's `AgentProvider.harness_overlay_lines`, since each harness installs
differently), and extra `files`, which land under ``OVERLAY_DEST`` in the image
preserving their relative path.

Or, instead of those, a free-form `dockerfile`: a single-stage Dockerfile that
builds `FROM ${BASE}`, staged with its own directory as the build context. The
staged copy pins `BASE` to the verified base (`ARG BASE=<ref@digest>`, so no
build-arg plumbing is needed per backend) and restores the provider's
`image_user` at the end.

Every backend builds from `staged_overlay`, whose `cache_key` hashes the
staged context, so they agree on when an overlay must rebuild.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .agent_provider import get_provider
from .manifest import ManifestError, ManifestImageOverlay
from .manifest.agent import APT_PACKAGE_RE, HARNESS_VERSION_RE

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
        # Re-checked here, not only at manifest parse: the names land in RUN,
        # and a newline would start a new Dockerfile instruction.
        for pkg in overlay.apt:
            if not APT_PACKAGE_RE.fullmatch(pkg):
                raise OverlayError(f"invalid apt package name {pkg!r}")
        pkgs = " ".join(overlay.apt)
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
        # JSON form: COPY doesn't do shell quoting, so a space would split it.
        lines.append(f"COPY {json.dumps([rel, dest])}")
    if runs:
        lines.append(f"USER {get_provider(template).image_user}")
    return "\n".join(lines) + "\n"


_FROM_BASE_RE = re.compile(r"\$(\{BASE\}|BASE)(\s+as\s+[A-Za-z0-9_.-]+)?", re.IGNORECASE)
_ARG_BASE_RE = re.compile(r"\s*ARG\s+BASE\s*", re.IGNORECASE)
# A parser directive (`# syntax=`, `# escape=`, `# check=`) changes how the
# file is parsed or which frontend parses it, which the checks below can't
# follow, so overlays refuse them.
_DIRECTIVE_RE = re.compile(r"\s*#\s*(syntax|escape|check)\s*=", re.IGNORECASE)
# BuildKit's own heredoc token shape: the whole word after `<<` (quotes
# stripped) is the terminator.
_HEREDOC_RE = re.compile(r"\d*<<(-?)([^<]+)")


def _physical_lines(text: str) -> list[str]:
    # Split only on newlines, as Docker does: `str.splitlines` also breaks on
    # \x85, \u2028 and friends, which would let the check and Docker disagree.
    return [ln.removesuffix("\r") for ln in text.split("\n")]


def _heredocs(rest: str) -> list[tuple[str, str]]:
    """(strip-tabs flag, terminator) for each heredoc an instruction opens.
    Any instruction counts, not just RUN/COPY/ADD: treating a non-heredoc as
    one can only make the check refuse a file, never accept a bad one."""
    out: list[tuple[str, str]] = []
    for token in rest.split():
        m = _HEREDOC_RE.fullmatch(token)
        if m:
            out.append((m.group(1), m.group(2).strip("\"'")))
    return out


def _instructions(lines: list[str]) -> list[tuple[int, str, str]]:
    """Logical instructions as (first physical line index, KEYWORD, rest),
    parsed the way Docker does: comments and blank lines dropped, including
    inside a backslash continuation, and the keyword split off on any
    whitespace. Heredoc bodies are skipped. A file that ends inside a
    continuation or an unterminated heredoc is refused, since whatever is
    appended after it (the restored USER) would be swallowed."""
    out: list[tuple[int, str, str]] = []
    buf: list[str] = []
    start = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if not buf:
            start = i - 1
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        joined = " ".join(buf).strip()
        buf = []
        parts = joined.split(None, 1)
        keyword, rest = parts[0].upper(), (parts[1] if len(parts) > 1 else "")
        out.append((start, keyword, rest))
        for strip_tabs, word in _heredocs(rest):
            while True:
                if i >= len(lines):
                    raise OverlayError(
                        f"overlay Dockerfile has an unterminated heredoc ({word})"
                    )
                body = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if body == word:
                    break
    if buf:
        raise OverlayError(
            "overlay Dockerfile ends inside a line continuation (trailing '\\')"
        )
    return out


def render_overlay_dockerfile_text(base_ref: str, text: str, *, template: str) -> str:
    """Validate a user overlay Dockerfile and return the staged copy.

    The Dockerfile must be single-stage and build `FROM ${BASE}` (or `$BASE`),
    so the image is always a layer on the verified base: only `ARG`s may come
    before that one `FROM`, and no other `FROM` may follow. `BASE` is pinned by
    an `ARG BASE=<base_ref>` prepended to the staged copy; the user's own bare
    `ARG BASE` line (before the FROM) is dropped, and any other declaration of
    `BASE` is an error, since it could replace the verified base. The
    provider's `image_user` is restored at the end, so a Dockerfile that
    switches to root can't leave the agent running as root.

    The checks are deliberately stricter than Docker: parser directives are
    refused, and any physical line that starts with FROM counts as a stage,
    even inside a heredoc or continuation, so the check can only over-count."""
    lines = _physical_lines(text)
    instrs = _instructions(lines)
    leading = itertools.takewhile(lambda ln: ln.strip().startswith("#"), lines)
    if any(_DIRECTIVE_RE.match(ln) for ln in leading):
        raise OverlayError(
            "overlay Dockerfile must not use parser directives (# syntax=, "
            "# escape=, # check=); they change how the file is parsed"
        )
    from_lines = [
        n for n, ln in enumerate(lines)
        if (ln.split(None, 1) or [""])[0].upper() == "FROM"
    ]
    if len(from_lines) != 1:
        raise OverlayError(
            f"overlay Dockerfile must have exactly one FROM (found {len(from_lines)}); "
            "multi-stage builds are not supported in an overlay"
        )
    froms = [(n, rest) for n, kw, rest in instrs if kw == "FROM"]
    if len(froms) != 1 or froms[0][0] != from_lines[0]:
        raise OverlayError("overlay Dockerfile's FROM must start its own instruction")
    from_line, from_rest = froms[0]
    if not _FROM_BASE_RE.fullmatch(from_rest):
        raise OverlayError(
            f"overlay Dockerfile must build FROM ${{BASE}} (got FROM {from_rest}); "
            "bot-bottle sets BASE to the verified, digest-pinned agent image"
        )
    drop: set[int] = set()
    for n, kw, rest in instrs:
        if n >= from_line:
            break
        if kw != "ARG":
            raise OverlayError(
                f"overlay Dockerfile has {kw} before FROM; only ARG may precede it"
            )
        try:
            names = [tok.split("=", 1)[0] for tok in shlex.split(rest)]
        except ValueError as e:
            raise OverlayError(f"overlay Dockerfile has an unparseable ARG: {e}") from e
        if any(name.upper() == "BASE" for name in names):
            if not _ARG_BASE_RE.fullmatch(lines[n]):
                raise OverlayError(
                    "overlay Dockerfile may only declare BASE as a bare `ARG BASE` "
                    "line; bot-bottle sets it to the verified, digest-pinned "
                    "agent image"
                )
            drop.add(n)
    kept = [ln for n, ln in enumerate(lines) if n not in drop]
    user = get_provider(template).image_user
    return (
        "# Staged by bot-bottle: BASE is the verified,\n"
        "# digest-pinned agent image; the provider's user is restored at the end.\n"
        f"ARG BASE={base_ref}\n"
        + "\n".join(kept).rstrip("\n")
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


def _source(source_root: Path, rel: str, kind: str) -> Path:
    """The host file an overlay entry names, refusing one that is missing or
    that resolves outside `source_root` through a symlink. `source_root` is
    the launch directory, which may be an untrusted checkout, so a link like
    `creds -> ~/.aws/credentials` must not copy a host secret into the image."""
    path = source_root / rel
    if not path.is_file():
        raise OverlayError(f"overlay {kind} not found: {rel!r} (looked in {source_root})")
    if not path.resolve().is_relative_to(source_root.resolve()):
        raise OverlayError(
            f"overlay {kind} {rel!r} resolves outside {source_root} (through a "
            "symlink); overlay inputs must live in the launch directory"
        )
    return path


def stage_overlay_context(
    base_ref: str,
    overlay: ManifestImageOverlay,
    *,
    template: str,
    source_root: Path,
    dest: Path,
) -> Path:
    """Write the overlay Dockerfile and copy its inputs into `dest`, returning
    the Dockerfile path. `source_root` is where the overlay's paths resolve
    from: the launch cwd, matching how a custom `agent_provider.dockerfile`
    resolves. Missing inputs fail loudly rather than producing an image that
    silently omits them."""
    dest.mkdir(parents=True, exist_ok=True)
    dockerfile = dest / STAGED_DOCKERFILE
    if overlay.dockerfile:
        src = _source(source_root, overlay.dockerfile, "dockerfile")
        staged = render_overlay_dockerfile_text(
            base_ref, src.read_text(), template=template)
        # The Dockerfile's own directory is the build context, so its COPYs
        # resolve next to it. Symlinks are copied as links, not followed: the
        # build then sees the link itself, as Docker would, and never the
        # host file it points at.
        shutil.copytree(src.parent, dest, symlinks=True, dirs_exist_ok=True)
        dockerfile.write_text(staged)
        return dockerfile
    dockerfile.write_text(render_overlay_dockerfile(base_ref, overlay, template=template))
    for rel in overlay.files:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_source(source_root, rel, "file"), out)
    return dockerfile


def context_digest(ctx: Path) -> str:
    """A digest of everything in a staged build context: the rendered
    Dockerfile and every input, with symlinks hashed by their target rather
    than followed. Keying caches on this (instead of the overlay spec) means
    any change to what the build would actually see rebuilds, including a
    change to how bot-bottle renders the Dockerfile."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(ctx):
        dirnames.sort()
        for name in sorted([*dirnames, *filenames]):
            path = Path(dirpath) / name
            rel = path.relative_to(ctx).as_posix()
            if path.is_symlink():
                h.update(b"L" + rel.encode() + b"\0" + os.readlink(path).encode() + b"\0")
            elif path.is_file():
                h.update(b"F" + rel.encode() + b"\0" + path.read_bytes() + b"\0")
            else:
                h.update(b"D" + rel.encode() + b"\0")
    return h.hexdigest()


@dataclass(frozen=True)
class StagedOverlay:
    """A staged overlay build: its context dir, the Dockerfile in it, and
    `cache_key`, which identifies the image it builds (base ref + context)."""

    context: Path
    dockerfile: Path
    cache_key: str


@contextmanager
def staged_overlay(
    base_ref: str,
    overlay: ManifestImageOverlay,
    *,
    template: str,
    source_root: Path,
) -> Generator[StagedOverlay, None, None]:
    """Stage the overlay in a temporary directory for the duration of the
    block. Every backend builds from this, so they agree on the cache key."""
    with tempfile.TemporaryDirectory(prefix="bb-overlay-") as tmp:
        ctx = Path(tmp) / "ctx"
        dockerfile = stage_overlay_context(
            base_ref, overlay, template=template, source_root=source_root, dest=ctx)
        h = hashlib.sha256(f"{base_ref}\0{context_digest(ctx)}".encode())
        yield StagedOverlay(ctx, dockerfile, h.hexdigest()[:16])


__all__ = [
    "OVERLAY_DEST",
    "STAGED_DOCKERFILE",
    "OverlayError",
    "StagedOverlay",
    "context_digest",
    "no_published_base_error",
    "render_overlay_dockerfile",
    "render_overlay_dockerfile_text",
    "stage_overlay_context",
    "staged_overlay",
]
