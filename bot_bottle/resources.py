"""Locate build resources in a source checkout or an installed wheel.

`build_root()` returns a directory shaped like a repo root (`bot_bottle/`,
`pyproject.toml`, the Dockerfiles, `nix/`, `scripts/`). A checkout returns the
repo root; a wheel stages one from the files bundled under
`bot_bottle/_resources/`.
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

_PKG = Path(__file__).resolve().parent  # .../bot_bottle
_CHECKOUT_ROOT = _PKG.parent  # repo root in a checkout
_BUNDLED = _PKG / "_resources"  # wheel-shipped copies

# Root-level files bundled into the wheel under `_resources/`, paths relative
# to the checkout root. Must match `setup.py` (`test_resources` checks).
BUNDLED_RESOURCES: tuple[str, ...] = (
    "pyproject.toml",
    "image-build-args.json",
    "Dockerfile.gateway",
    "Dockerfile.orchestrator",
    "Dockerfile.orchestrator.fc",
    "requirements.gateway.lock",
    "requirements.orchestrator.lock",
    "nix/firecracker-netpool.nix",
    "scripts/firecracker-netpool.sh",
)

# Bundled only when present at build time: the artifacts manifest exists only
# after a release. Must match `setup.py`'s `_OPTIONAL_BUNDLED_RESOURCES`.
OPTIONAL_BUNDLED_RESOURCES: tuple[str, ...] = (
    "published-artifacts.json",
)

# Present at a checkout root, never in an installed package.
_CHECKOUT_MARKER = "Dockerfile.gateway"
_IMAGE_BUILD_ARGS_FILE = "image-build-args.json"
_CENTRAL_BUILD_ARG_NAMES = frozenset({
    "DOCKER_CLI_BASE_IMAGE",
    "NODE_BASE_IMAGE",
    "PYTHON_BASE_IMAGE",
})


class ResourceError(RuntimeError):
    """Build resources are missing from the install."""


def is_source_checkout() -> bool:
    """True when running from a source tree."""
    return (_CHECKOUT_ROOT / _CHECKOUT_MARKER).is_file()


def build_root() -> Path:
    """A repo-root-shaped directory: the checkout root, or a copy staged from
    the wheel once and reused. The stage is keyed by content digest, so a
    reinstall at the same version re-stages."""
    if is_source_checkout():
        return _CHECKOUT_ROOT
    return _stage_build_root()


def dockerfile(name: str) -> Path:
    """Absolute path to a root-level Dockerfile, e.g. `Dockerfile.gateway`."""
    return build_root() / name


def image_build_args(
    dockerfile_path: str | Path,
    *,
    context: str | Path | None = None,
) -> dict[str, str]:
    """The centralized build args `dockerfile_path` declares (digest-pinned
    bases with no Dockerfile defaults). Callers' explicit args may override
    them.
    """
    path = Path(dockerfile_path)
    if not path.is_absolute() and context is not None:
        path = Path(context) / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # A Dockerfile outside bot-bottle; the builder reports a missing file.
        return {}
    declared = set(re.findall(
        r"(?m)^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)\s*$",
        text,
    ))
    wanted = declared & _CENTRAL_BUILD_ARG_NAMES
    if not wanted:
        return {}
    # Generated Dockerfiles live in temporary contexts; use that context's
    # input file if it has one, else the build root's.
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
    """The firecracker netpool NixOS module."""
    return build_root() / "nix" / "firecracker-netpool.nix"


def netpool_script() -> Path:
    """The firecracker netpool setup script."""
    return build_root() / "scripts" / "firecracker-netpool.sh"


def _content_digest() -> str:
    """16-hex digest of the installed package (which includes `_resources`)."""
    h = hashlib.sha256()
    for path in sorted(_PKG.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(_PKG)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _stage_build_root() -> Path:
    """Stage a repo-root-shaped build context from the wheel, keyed by content
    digest. Serialized by a file lock and published by atomic rename."""
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

    # Serialize staging across processes; the fast path above never blocks.
    with open(base / ".stage.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (dest / ".complete").is_file():  # another process staged while we waited
            return dest
        # Stage privately on the same filesystem, then publish by rename.
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=base))
        try:
            # The package minus caches and bundled copies, matching a
            # checkout's layout.
            shutil.copytree(
                _PKG,
                staging / "bot_bottle",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_resources"),
            )
            # Bundled root files, in their checkout layout.
            for rel in BUNDLED_RESOURCES:
                dst = staging / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(_BUNDLED / rel, dst)
            # Optional root files, when the wheel shipped them.
            for rel in OPTIONAL_BUNDLED_RESOURCES:
                src = _BUNDLED / rel
                if src.is_file():
                    dst = staging / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            (staging / ".complete").write_text("")
            # Replace a partial leftover (we hold the lock), then publish.
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(staging, dest)
            staging = None  # published
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
    return dest
