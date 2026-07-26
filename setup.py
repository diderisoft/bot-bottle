"""Build shim. Project metadata lives in ``pyproject.toml``; this only adds a
build step that copies the root-level build resources (the Dockerfiles, the
nix netpool module, the netpool script, and ``pyproject.toml``) into
``bot_bottle/_resources/`` so an installed wheel is self-contained and can
build its gateway/infra/orchestrator images without a source checkout.

Kept in sync with ``bot_bottle.resources.BUNDLED_RESOURCES`` — the
``test_resources`` suite guards against drift between the two lists.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

_ROOT = Path(__file__).resolve().parent

# Must match bot_bottle.resources.BUNDLED_RESOURCES (paths relative to root).
_BUNDLED_RESOURCES = (
    "pyproject.toml",
    "Dockerfile.gateway",
    "Dockerfile.orchestrator",
    "Dockerfile.orchestrator.fc",
    "nix/firecracker-netpool.nix",
    "scripts/firecracker-netpool.sh",
)


class _BundleResources(build_py):
    """Copy the root-level build resources into the built package tree so they
    ship inside the wheel under ``bot_bottle/_resources/``."""

    def run(self) -> None:
        super().run()
        pkg_resources = Path(self.build_lib) / "bot_bottle" / "_resources"
        for rel in _BUNDLED_RESOURCES:
            src = _ROOT / rel
            dst = pkg_resources / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


setup(cmdclass={"build_py": _BundleResources})
