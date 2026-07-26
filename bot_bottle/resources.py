"""Locate build-time resources whether bot-bottle runs from a source
checkout or an installed wheel.

The gateway / infra / orchestrator images are built from a Docker (or Apple
`container`) build context that must contain the `bot_bottle` package,
`pyproject.toml`, and the root-level Dockerfiles as siblings. In a source
checkout that context is simply the repo root, one level above the package.
An installed wheel has no repo root: the same root-level files are shipped
inside the package under ``bot_bottle/_resources/`` (see ``setup.py``), and a
repo-root-shaped build context is staged on demand into the app-data dir.

``build_root()`` is the single source of truth — it returns a directory laid
out like a repo root (has ``bot_bottle/``, ``pyproject.toml``, the
Dockerfiles, ``nix/``, ``scripts/``). Every caller that needs a build
context, a Dockerfile path, the nix netpool module, or the netpool script
derives from it, so checkout and wheel installs share one downstream path.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .paths import bot_bottle_root

_PKG = Path(__file__).resolve().parent          # …/bot_bottle
_CHECKOUT_ROOT = _PKG.parent                     # repo root in a checkout
_BUNDLED = _PKG / "_resources"                   # wheel-shipped copies

# Root-level files bundled into the wheel under ``_resources/`` (paths are
# relative to the checkout root, and preserved verbatim under ``_resources/``
# and in the staged build root). ``setup.py`` copies exactly this set; keep
# the two lists in sync (``test_resources`` guards that every entry exists).
BUNDLED_RESOURCES: tuple[str, ...] = (
    "pyproject.toml",
    "image-build-args.json",
    "Dockerfile.gateway",
    "Dockerfile.orchestrator",
    "Dockerfile.orchestrator.fc",
    "requirements.gateway.lock",
    "nix/firecracker-netpool.nix",
    "scripts/firecracker-netpool.sh",
)

# Present at a checkout root, never in a bare installed package — the cheap
# tell for which layout we're in.
_CHECKOUT_MARKER = "Dockerfile.gateway"
_IMAGE_BUILD_ARGS_FILE = "image-build-args.json"
_CENTRAL_BUILD_ARG_NAMES = frozenset({
    "DOCKER_CLI_BASE_IMAGE",
    "NODE_BASE_IMAGE",
    "PYTHON_BASE_IMAGE",
})


class ResourceError(RuntimeError):
    """Build resources are missing from the install (corrupt/partial wheel)."""


def is_source_checkout() -> bool:
    """True when running from a source tree (the root Dockerfiles sit beside
    the package); False from an installed wheel."""
    return (_CHECKOUT_ROOT / _CHECKOUT_MARKER).is_file()


def build_root() -> Path:
    """A directory shaped like a repo root: ``bot_bottle/``, ``pyproject.toml``,
    the root Dockerfiles, ``nix/``, and ``scripts/``.

    A checkout returns the repo root itself (no copying). An installed wheel
    returns a staged copy under the app-data dir, materialized once and reused.
    The stage is keyed by a digest of the installed package + bundled resources
    (not the distribution version), so a force-reinstall of a newer commit that
    keeps ``version = 0.1.0`` still rebuilds instead of reusing a stale tree."""
    if is_source_checkout():
        return _CHECKOUT_ROOT
    return _stage_build_root()


def dockerfile(name: str) -> Path:
    """Absolute path to a root-level Dockerfile, e.g. ``Dockerfile.gateway``."""
    return build_root() / name


def image_build_args(
    dockerfile_path: str | Path,
    *,
    context: str | Path | None = None,
) -> dict[str, str]:
    """Return centralized arguments declared by ``dockerfile_path``.

    Base-image arguments deliberately have no Dockerfile defaults. Their
    digest-pinned values live in one repository input file and every supported
    build path calls this helper before invoking its OCI builder. Explicit
    caller-supplied arguments may still override this returned mapping.
    """
    path = Path(dockerfile_path)
    if not path.is_absolute() and context is not None:
        path = Path(context) / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Generic callers and tests may build an ephemeral Dockerfile outside
        # bot-bottle. The builder will report a genuinely missing file.
        return {}
    declared = set(re.findall(
        r"(?m)^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)\s*$",
        text,
    ))
    wanted = declared & _CENTRAL_BUILD_ARG_NAMES
    if not wanted:
        return {}
    # Generated Dockerfiles (notably the macOS nested-container layer) live in
    # temporary build contexts. Resolve their declaration there, but take the
    # centralized values from that context only when it carries its own input
    # file; otherwise use bot-bottle's staged build root.
    context_root = Path(context) if context is not None else None
    root = (
        context_root
        if context_root is not None
        and (context_root / _IMAGE_BUILD_ARGS_FILE).is_file()
        else build_root()
    )
    inputs_path = root / _IMAGE_BUILD_ARGS_FILE
    try:
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceError(
            f"cannot read centralized image build arguments from {inputs_path}: {exc}"
        ) from exc
    if not isinstance(inputs, dict):
        raise ResourceError(f"{inputs_path} must contain a JSON object")
    missing = wanted - inputs.keys()
    if missing:
        raise ResourceError(
            f"{inputs_path} lacks required image build arguments: "
            f"{', '.join(sorted(missing))}"
        )
    invalid = [name for name in wanted if not isinstance(inputs[name], str)]
    if invalid:
        raise ResourceError(
            f"{inputs_path} has non-string image build arguments: "
            f"{', '.join(sorted(invalid))}"
        )
    return {name: inputs[name] for name in sorted(wanted)}


def nix_netpool_module() -> Path:
    """Absolute path to the firecracker netpool NixOS module."""
    return build_root() / "nix" / "firecracker-netpool.nix"


def netpool_script() -> Path:
    """Absolute path to the firecracker netpool bring-up script."""
    return build_root() / "scripts" / "firecracker-netpool.sh"


def _content_digest() -> str:
    """A 16-hex digest of the installed package + bundled resources.

    Keys the staged build root by *content*, so a force-reinstall over the same
    version string (the installer defaults to a git branch + ``pipx install
    --force``, and ``version`` stays ``0.1.0``) yields a different key and
    re-stages, rather than reusing an old commit's tree. ``_PKG`` already
    contains ``_resources``, so walking it covers both."""
    h = hashlib.sha256()
    for path in sorted(_PKG.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(_PKG)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _stage_build_root() -> Path:
    """Materialize a repo-root-shaped build context from the installed wheel's
    bundled resources, keyed by content digest. Idempotent and concurrency-safe:
    a file lock serializes staging, a partial/stale tree is replaced, and the
    finished tree is published with an atomic rename."""
    if not _BUNDLED.is_dir():
        raise ResourceError(
            "bot-bottle build resources are missing from this install "
            f"(expected {_BUNDLED}). Reinstall the package."
        )
    base = bot_bottle_root() / "build-root"
    base.mkdir(parents=True, exist_ok=True)
    dest = base / _content_digest()
    if (dest / ".complete").is_file():
        return dest

    # Serialize staging across processes: a concurrent `start` after an install
    # must not race on the shared tree. The lock is held only around stage +
    # atomic publish; the fast path above never blocks.
    with open(base / ".stage.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (dest / ".complete").is_file():  # another process staged while we waited
            return dest
        # Stage into a private temp dir on the same filesystem, then publish by
        # rename — never populate a shared path other processes might read.
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=base))
        try:
            # The package itself, minus caches and the bundled-resource copies,
            # so the staged ``bot_bottle/`` matches a checkout's (keeps the
            # firecracker infra-artifact hash stable across checkout and wheel).
            shutil.copytree(
                _PKG,
                staging / "bot_bottle",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_resources"),
            )
            # The bundled root files, restored to their checkout-relative layout.
            for rel in BUNDLED_RESOURCES:
                dst = staging / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(_BUNDLED / rel, dst)
            (staging / ".complete").write_text("")
            # Replace any partial leftover for this digest (safe: we hold the
            # lock), then publish atomically.
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(staging, dest)
            staging = None  # published; nothing to clean up
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
    return dest
