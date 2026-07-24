"""Backend selection + readiness-aware skip guards for the integration suite.

Each integration test targets the backend named by ``BOT_BOTTLE_BACKEND``
(default ``docker``) and gates on that backend's full readiness check —
``is_backend_ready()`` (equivalent to ``./cli.py backend status``), not just
a binary-on-PATH probe. When the backend is not ready, diagnostic output is
printed during test discovery so the operator sees a concrete reason for each
skip.
"""

from __future__ import annotations

import os
import unittest

from bot_bottle.backend import is_backend_ready

# Default when ``BOT_BOTTLE_BACKEND`` is unset. Docker preserves the historical
# Docker-backed CI path (and mirrors the pin in ``test_sandbox_escape``).
DEFAULT_BACKEND = "docker"


def selected_backend() -> str:
    """The backend this test run targets, from ``BOT_BOTTLE_BACKEND``.

    Mirrors the CLI's env selector; unset means ``docker`` so an
    unconfigured run behaves exactly as the suite did before backends were
    pluggable.
    """
    return os.environ.get("BOT_BOTTLE_BACKEND") or DEFAULT_BACKEND


def skip_unless_backend(backend: str):
    """Skip a backend-specific test unless the selected backend matches AND
    that backend is fully ready.

    Docker-implementation tests (``DockerBroker``, ``DockerGateway``,
    ``backend.docker.*``) use ``skip_unless_backend("docker")`` so they no-op
    under a run targeting a different backend instead of testing Docker
    internals that run doesn't exercise — the guard reads
    ``BOT_BOTTLE_BACKEND`` rather than "is Docker installed".

    When the backend is not ready, ``status()`` output is printed so the
    operator sees a concrete diagnostic for each skipped test module.
    """
    sel = selected_backend()
    if sel != backend:
        return unittest.skip(
            f"backend {backend!r} not selected (BOT_BOTTLE_BACKEND={sel})"
        )
    return unittest.skipUnless(
        is_backend_ready(backend, quiet=False),
        f"{backend} backend not ready",
    )


def skip_unless_selected_backend_available():
    """Skip a backend-agnostic test unless the *selected* backend is fully ready.

    The test then runs through whichever backend ``BOT_BOTTLE_BACKEND`` names,
    gated on that backend's full status() check (e.g. daemon reachable, TAP
    pool present for Firecracker) rather than just a binary-on-PATH probe.

    When the backend is not ready, ``status()`` output is printed so the
    operator sees a concrete diagnostic for each skipped test module.
    """
    backend = selected_backend()
    return unittest.skipUnless(
        is_backend_ready(backend, quiet=False),
        f"selected backend {backend!r} not ready",
    )
