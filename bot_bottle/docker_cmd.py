"""Lean, framework-free `docker` subprocess primitive.

Deliberately a top-level module with a single stdlib import so it can be
reused from anywhere without cost. It is intentionally *not* placed in
`backend.docker.util`: importing that module runs `backend/__init__.py`,
which eagerly loads all three bottle backends (docker + firecracker +
macos) plus the manifest/egress/git-gate/supervise framework — ~76 modules
— which would drag the whole backend layer into the deliberately-lean
orchestrator. This primitive stays free of that so both the orchestrator's
docker components and (in time) `backend.docker.util` can share it."""

from __future__ import annotations

import subprocess


def run_docker(
    argv: list[str], *, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a `docker` command, capturing stdout/stderr as text. Never raises
    on a non-zero exit — callers inspect `returncode` / `stderr` so they can
    stay fail-closed or tolerate idempotent no-ops (e.g. removing an
    already-absent container).

    `env` sets the child process environment — used to hand a secret to a bare
    `--env NAME` flag (docker inherits its value from this process) so the
    value never lands on argv or in `docker inspect`'s recorded command line."""
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        check=False, env=env,
    )


__all__ = ["run_docker"]
