"""Backend-aware capability probes and skip guards for the integration suite.

Every integration test selects its backend from ``BOT_BOTTLE_BACKEND``
(default ``docker``) and exercises the full test through that backend. Skip
guards check the *capability* the selected backend actually needs — a
reachable Docker daemon for ``docker``, an accessible ``/dev/kvm`` plus a
``firecracker`` binary for ``firecracker`` — rather than gating every backend
on unrelated Docker availability.

The same probes back the CI preflight (``python3 -m tests.backend_preflight``)
so a job surfaces missing infrastructure at the job level with a clear
PASS/FAIL line instead of turning every test into a silent ``unittest.skip``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from dataclasses import dataclass

# Default when ``BOT_BOTTLE_BACKEND`` is unset. Docker preserves the historical
# Docker-backed CI path (and mirrors the pin in ``test_sandbox_escape``).
DEFAULT_BACKEND = "docker"

# `/dev/kvm` must exist and be openable by the invoking user for the
# Firecracker backend to boot a guest (mirrors
# ``bot_bottle.backend.firecracker.util``).
_KVM_DEVICE = "/dev/kvm"


def selected_backend() -> str:
    """The backend this test run targets, from ``BOT_BOTTLE_BACKEND``.

    Mirrors the CLI's env selector; unset means ``docker`` so an
    unconfigured run behaves exactly as the suite did before backends were
    pluggable.
    """
    return os.environ.get("BOT_BOTTLE_BACKEND") or DEFAULT_BACKEND


@dataclass(frozen=True)
class Capability:
    """Outcome of a backend capability probe.

    ``ok`` gates the tests; ``detail`` is a one-line human string reused for
    both preflight output and ``unittest.skip`` reasons so the same wording
    shows up at the job level and next to a skipped test.
    """

    backend: str
    ok: bool
    detail: str


def docker_capability() -> Capability:
    """Whether a Docker daemon is reachable (and not opted out)."""
    name = "docker"
    if os.environ.get("SKIP_DOCKER_TESTS"):
        return Capability(name, False, "SKIP_DOCKER_TESTS is set")
    if shutil.which("docker") is None:
        return Capability(name, False, "docker not on PATH")
    try:
        returncode = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        ).returncode
    except subprocess.TimeoutExpired:
        return Capability(name, False, "docker info timed out")
    if returncode != 0:
        return Capability(name, False, "docker daemon unreachable")
    return Capability(name, True, "docker daemon reachable")


def firecracker_capability() -> Capability:
    """Whether this host can boot a Firecracker guest: an accessible
    ``/dev/kvm`` and a ``firecracker`` binary on ``PATH``.

    Deliberately does not probe unrelated Docker availability — the
    Firecracker backend is Docker-independent at runtime.
    """
    name = "firecracker"
    if not os.path.exists(_KVM_DEVICE):
        return Capability(name, False, f"{_KVM_DEVICE} missing — KVM unavailable")
    if not os.access(_KVM_DEVICE, os.R_OK | os.W_OK):
        return Capability(
            name, False, f"{_KVM_DEVICE} not accessible — add your user to the kvm group"
        )
    if shutil.which("firecracker") is None:
        return Capability(name, False, "firecracker not on PATH")
    return Capability(name, True, f"{_KVM_DEVICE} accessible; firecracker on PATH")


_PROBES = {
    "docker": docker_capability,
    "firecracker": firecracker_capability,
}


def backend_capability(backend: str | None = None) -> Capability:
    """Probe ``backend`` (default: the selected backend) for readiness."""
    backend = backend or selected_backend()
    probe = _PROBES.get(backend)
    if probe is None:
        return Capability(backend, False, f"no capability probe for backend {backend!r}")
    return probe()


# ---- back-compat boolean probe -------------------------------------------


def docker_available() -> bool:
    """Boolean Docker probe (kept for callers that only need the flag)."""
    return docker_capability().ok


# ---- skip guards ---------------------------------------------------------


def skip_unless_backend(backend: str):
    """Skip a backend-specific test unless the selected backend matches AND
    that backend's capability is present.

    Docker-implementation tests (``DockerBroker``, ``DockerGateway``,
    ``backend.docker.*``) use ``skip_unless_backend("docker")`` so they no-op
    under a Firecracker run instead of testing Docker internals that run
    doesn't target — the guard reads ``BOT_BOTTLE_BACKEND`` rather than
    "is Docker installed".
    """
    sel = selected_backend()
    if sel != backend:
        return unittest.skip(
            f"backend {backend!r} not selected (BOT_BOTTLE_BACKEND={sel})"
        )
    cap = backend_capability(backend)
    return unittest.skipUnless(cap.ok, f"{backend} backend unavailable: {cap.detail}")


def skip_unless_selected_backend_available():
    """Skip a backend-agnostic test unless the *selected* backend can run it.

    The test then exercises whichever backend ``BOT_BOTTLE_BACKEND`` names,
    checking that backend's real capability (e.g. ``/dev/kvm`` for
    Firecracker) rather than unrelated Docker availability.
    """
    cap = backend_capability()
    return unittest.skipUnless(
        cap.ok, f"selected backend {cap.backend!r} unavailable: {cap.detail}"
    )
