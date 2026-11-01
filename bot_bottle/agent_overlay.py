"""Render, validate and stage overlay builds on a pinned agent image.

An overlay is either directives (`apt`, `harness_version`, `files` under
`OVERLAY_DEST`) or a single-stage inline `dockerfile` that builds `FROM
${BASE}`. Both build on the verified base and end as the provider's
`image_user`.
`staged_overlay`'s `cache_key` hashes the staged context, so every backend
agrees on when to rebuild. `inputs_digest` hashes the same staged context
minus the base, for a project agent's trust hash.
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

# Where overlay `files` land in the image.
OVERLAY_DEST = "/opt/bot-bottle/overlay"

# Staged overlay Dockerfile name inside the build context.
STAGED_DOCKERFILE = ".bb-overlay.Dockerfile"


class OverlayError(ManifestError):
    """An overlay can't be rendered or staged. A ManifestError, so the CLI
    reports it as a config problem."""


def no_published_base_error(harness: str) -> OverlayError:
    """Error for an overlay with no pinned base to build on. Fails closed
    rather than silently building the default image without the overlay."""
    return OverlayError(
        f"agent_provider.overlay needs a published, digest-pinned "
        f"{harness!r} agent image to build on, but none is pinned for this "
        "install (no published-artifacts.json, or no entry for this harness). "
        "Remove the overlay, or use a release that publishes agent images."
    )


def render_overlay_dockerfile(
    base_ref: str, overlay: ManifestImageOverlay, *, harness: str,
) -> str:
    """The Dockerfile that layers `overlay` on the digest-pinned `base_ref`.
    RUN steps run as root; the provider's `image_user` is restored afterwards."""
    lines = [f"FROM {base_ref}"]
    runs: list[str] = []
    if overlay.apt:
        # Re-checked: a newline in a name would start a new instruction.
        for pkg in overlay.apt:
            if not APT_PACKAGE_RE.fullmatch(pkg):
                raise OverlayError(f"invalid apt package name {pkg!r}")
        pkgs = " ".join(overlay.apt)
        # Match the base images' apt flag for snapshot.debian.org's expired
        # Release files.
        runs.append(
            "RUN apt-get -o Acquire::Check-Valid-Until=false update "
            f"&& apt-get install -y --no-install-recommends {pkgs} "
            "&& rm -rf /var/lib/apt/lists/*"
        )
    if overlay.harness_version:
        runs.extend(_harness_lines(harness, overlay.harness_version))
    if runs:
        lines.append("USER root")
        lines.extend(runs)
    for rel in overlay.files:
        dest = f"{OVERLAY_DEST}/{rel}"
        # JSON form: COPY doesn't shell-quote.
        lines.append(f"COPY {json.dumps([rel, dest])}")
    if runs:
        lines.append(f"USER {get_provider(harness).image_user}")
    return "\n".join(lines) + "\n"


_FROM_BASE_RE = re.compile(r"\$(\{BASE\}|BASE)(\s+as\s+[A-Za-z0-9_.-]+)?", re.IGNORECASE)
_ARG_BASE_RE = re.compile(r"\s*ARG\s+BASE\s*", re.IGNORECASE)
# Parser directives change how the file is parsed, which the checks below can't
# follow, so they are refused.
_DIRECTIVE_RE = re.compile(r"\s*#\s*(syntax|escape|check)\s*=", re.IGNORECASE)
# BuildKit's heredoc token: the word after `<<` (quotes stripped) is the
# terminator.
_HEREDOC_RE = re.compile(r"\d*<<(-?)([^<]+)")


def _physical_lines(text: str) -> list[str]:
    # Split only on newlines, as Docker does (`str.splitlines` also splits on
    # \x85, \u2028, ...).
    return [ln.removesuffix("\r") for ln in text.split("\n")]


def _heredocs(rest: str) -> list[tuple[str, str]]:
    """(strip-tabs flag, terminator) for each heredoc an instruction opens. Any
    instruction counts, so the check can only over-refuse."""
    out: list[tuple[str, str]] = []
    for token in rest.split():
        m = _HEREDOC_RE.fullmatch(token)
        if m:
            out.append((m.group(1), m.group(2).strip("\"'")))
    return out


def _instructions(lines: list[str]) -> list[tuple[int, str, str]]:
    """Logical instructions as (first line index, KEYWORD, rest), parsed as
    Docker does. Heredoc bodies are skipped. A file ending inside a
    continuation or an unterminated heredoc is refused, since the appended USER
    would be swallowed."""
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


def render_overlay_dockerfile_text(base_ref: str, text: str, *, harness: str) -> str:
    """Validate a user overlay Dockerfile and return the staged copy.

    It must be single-stage and build `FROM ${BASE}` (only `ARG`s before it).
    `BASE` is pinned by a prepended `ARG BASE=<base_ref>`; the user's bare `ARG
    BASE` is dropped and any other `BASE` declaration is an error. The
    provider's `image_user` is restored at the end.

    Stricter than Docker: parser directives are refused, and any physical line
    starting with FROM counts as a stage."""
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
    user = get_provider(harness).image_user
    return (
        "# Staged by bot-bottle: BASE is the verified,\n"
        "# digest-pinned agent image; the provider's user is restored at the end.\n"
        f"ARG BASE={base_ref}\n"
        + "\n".join(kept).rstrip("\n")
        + f"\nUSER {user}\n"
    )


def _harness_lines(harness: str, version: str) -> tuple[str, ...]:
    # Re-checked: the version lands in RUN.
    if not HARNESS_VERSION_RE.fullmatch(version):
        raise OverlayError(f"invalid harness_version {version!r}")
    lines = get_provider(harness).harness_overlay_lines(version)
    if lines is None:
        raise OverlayError(
            f"agent provider {harness!r} does not support "
            "agent_provider.overlay.harness_version; "
            "pin the harness in a custom agent image instead"
        )
    return lines


def _source(source_root: Path, rel: str, kind: str) -> Path:
    """The host file an overlay entry names. Refuses one that is missing or
    that resolves outside `source_root` (possibly an untrusted checkout)
    through a symlink."""
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
    harness: str,
    source_root: Path,
    dest: Path,
) -> Path:
    """Write the overlay Dockerfile and copy its inputs into `dest`; returns
    the Dockerfile path. Paths resolve from `source_root` (the launch cwd)."""
    dest.mkdir(parents=True, exist_ok=True)
    dockerfile = dest / STAGED_DOCKERFILE
    if overlay.dockerfile:
        staged = render_overlay_dockerfile_text(
            base_ref, overlay.dockerfile, harness=harness)
        dockerfile.write_text(staged)
        return dockerfile
    dockerfile.write_text(render_overlay_dockerfile(base_ref, overlay, harness=harness))
    for rel in overlay.files:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_source(source_root, rel, "file"), out)
    return dockerfile


def context_digest(ctx: Path) -> str:
    """Digest of a staged build context: the rendered Dockerfile and every
    input, symlinks hashed by target. Any change to what the build would see
    rebuilds."""
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


# Stands in for the base ref when staging for `inputs_digest`: the trust hash
# covers what the project supplies, not which release's base it builds on.
_INPUTS_BASE = "bot-bottle-trust-base"


def inputs_digest(
    overlay: ManifestImageOverlay, *, harness: str, source_root: Path,
) -> str:
    """Digest of everything an overlay build takes from `source_root`: the
    context `staged_overlay` builds, staged on a placeholder base, so it covers
    exactly what the cache key does. An overlay that can't be staged (a
    missing file, a symlink out of `source_root`, a refused Dockerfile) digests
    its error: it can't build, and fixing it changes the digest. Empty for an
    empty overlay."""
    if overlay.is_empty():
        return ""
    with tempfile.TemporaryDirectory(prefix="bb-overlay-inputs-") as tmp:
        ctx = Path(tmp) / "ctx"
        try:
            stage_overlay_context(
                _INPUTS_BASE, overlay, harness=harness, source_root=source_root, dest=ctx)
        except OverlayError as e:
            return hashlib.sha256(f"unstageable\0{e}".encode()).hexdigest()
        except OSError as e:
            # Not the message: it can name the temporary directory.
            return hashlib.sha256(f"unstageable\0{type(e).__name__}".encode()).hexdigest()
        return context_digest(ctx)


@dataclass(frozen=True)
class StagedOverlay:
    """A staged overlay build: context dir, Dockerfile, and `cache_key` (base
    ref + context)."""

    context: Path
    dockerfile: Path
    cache_key: str


@contextmanager
def staged_overlay(
    base_ref: str,
    overlay: ManifestImageOverlay,
    *,
    harness: str,
    source_root: Path,
) -> Generator[StagedOverlay, None, None]:
    """Stage the overlay in a temporary directory for the duration of the
    block."""
    with tempfile.TemporaryDirectory(prefix="bb-overlay-") as tmp:
        ctx = Path(tmp) / "ctx"
        dockerfile = stage_overlay_context(
            base_ref, overlay, harness=harness, source_root=source_root, dest=ctx)
        h = hashlib.sha256(f"{base_ref}\0{context_digest(ctx)}".encode())
        yield StagedOverlay(ctx, dockerfile, h.hexdigest()[:16])


__all__ = [
    "OVERLAY_DEST",
    "STAGED_DOCKERFILE",
    "OverlayError",
    "StagedOverlay",
    "context_digest",
    "inputs_digest",
    "no_published_base_error",
    "render_overlay_dockerfile",
    "render_overlay_dockerfile_text",
    "stage_overlay_context",
    "staged_overlay",
]
