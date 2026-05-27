"""Sidecar bundle constants + helpers for the Docker backend
(PRD 0024 chunk 2).

The bundle image (built by Dockerfile.sidecars, see PRD 0024
chunk 1) collapses pipelock + egress + git-gate + supervise into
one container per bottle. Whether the renderer emits the bundle
shape (one `sidecars` service) or the legacy four-sidecar shape
is controlled by `CLAUDE_BOTTLE_SIDECAR_BUNDLE`; chunk 2 ships
both shapes side by side behind the flag so existing operators
keep working unchanged while the bundle path soaks.

This module is intentionally tiny — just the constants + the
flag + the container-name helper. The compose-renderer branch
that consumes it lives in `compose.py`.
"""

from __future__ import annotations

import os


# Bundle image. Defaults to a built-locally tag (built from the
# repo's Dockerfile.sidecars via compose `build:`). Operators
# pinning to a published digest can override via env, matching
# the existing `CLAUDE_BOTTLE_PIPELOCK_IMAGE` shape.
SIDECAR_BUNDLE_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_SIDECAR_IMAGE",
    "claude-bottle-sidecars:latest",
)

SIDECAR_BUNDLE_DOCKERFILE = "Dockerfile.sidecars"


def sidecar_bundle_container_name(slug: str) -> str:
    """`claude-bottle-sidecars-<slug>`. Same prefix scheme as the
    per-sidecar containers it replaces, so the dashboard's
    discovery-by-prefix logic keeps working."""
    return f"claude-bottle-sidecars-{slug}"


def sidecar_bundle_enabled(env: dict[str, str] | None = None) -> bool:
    """Feature-flag check. The flag is opt-in for chunk 2:
    unset / "" / "0" / "false" → legacy four-sidecar shape;
    anything else → bundle shape. Chunks 4-5 flip the default and
    then delete the flag.

    `env` defaults to `os.environ` at call time so tests can
    monkey-patch the environment without re-importing the module."""
    if env is None:
        env = dict(os.environ)
    raw = env.get("CLAUDE_BOTTLE_SIDECAR_BUNDLE", "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")
