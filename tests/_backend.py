"""Backend selection + capability-aware skip guards for the integration suite.

Each integration test targets the backend named by ``BOT_BOTTLE_BACKEND``
(default ``docker``) and skips on that backend's own readiness check —
``bot_bottle.backend.has_backend`` (the same probe behind
``./cli.py backend status``), not an unrelated "is Docker installed" test.
"""

from __future__ import annotations

import os
import unittest

from bot_bottle.backend import has_backend

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
    that backend is available on the host.

    Docker-implementation tests (``DockerBroker``, ``DockerGateway``,
    ``backend.docker.*``) use ``skip_unless_backend("docker")`` so they no-op
    under a run targeting a different backend instead of testing Docker
    internals that run doesn't exercise — the guard reads
    ``BOT_BOTTLE_BACKEND`` rather than "is Docker installed".
    """
    sel = selected_backend()
    if sel != backend:
        return unittest.skip(
            f"backend {backend!r} not selected (BOT_BOTTLE_BACKEND={sel})"
        )
    return unittest.skipUnless(
        has_backend(backend), f"{backend} backend prerequisites unavailable"
    )


def skip_unless_selected_backend_available():
    """Skip a backend-agnostic test unless the *selected* backend is available.

    The test then runs through whichever backend ``BOT_BOTTLE_BACKEND`` names,
    gated on that backend's real prerequisites (e.g. Linux + KVM for
    Firecracker) rather than unrelated Docker availability.
    """
    backend = selected_backend()
    return unittest.skipUnless(
        has_backend(backend), f"selected backend {backend!r} prerequisites unavailable"
    )
