"""Shared orchestrator-process constants + helpers (PRD 0070).

Backend-neutral pieces the per-backend infra services build on: the
control-plane port, the startup timeout, the start-error, and the source hash
used to detect a code change. The per-backend infra lifecycle lives with each
backend — docker: `backend.docker.infra.DockerInfraService`; macOS:
`backend.macos_container.infra`; firecracker: `backend.firecracker.infra_vm`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_PORT = 8099
DEFAULT_STARTUP_TIMEOUT_SECONDS = 45.0


class OrchestratorStartError(RuntimeError):
    """The infra container/VM did not become healthy within the timeout."""


def source_hash(repo_root: Path) -> str:
    """Content hash of the orchestrator's bind-mounted Python source (the
    `bot_bottle` package the control-plane process imports). Changes only
    when the code that would actually run changes — a backend's `ensure_running`
    recreates the container on a mismatch so a code change takes effect,
    but leaves a healthy up-to-date container alone to preserve in-memory
    egress tokens."""
    h = hashlib.sha256()
    for path in sorted((repo_root / "bot_bottle").rglob("*.py")):
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "OrchestratorStartError",
    "source_hash",
]
