"""Shared `docker` subprocess helper for the orchestrator's docker-backed
components (the launch broker and the consolidated sidecar)."""

from __future__ import annotations

import subprocess


def run_docker(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a `docker` command, capturing stdout/stderr as text. Never raises
    on a non-zero exit — callers inspect `returncode` / `stderr` so they can
    stay fail-closed or tolerate idempotent no-ops (e.g. removing an
    already-absent container)."""
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )


__all__ = ["run_docker"]
