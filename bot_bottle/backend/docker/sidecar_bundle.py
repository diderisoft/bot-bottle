"""Sidecar bundle constants + helpers for the Docker backend
(PRD 0024).

The bundle image (built by Dockerfile.sidecars, PRD 0024 chunk 1)
runs egress + git-gate + supervise as one container per bottle
under a small Python init supervisor. As of chunk 5 the bundle
is the only shape — the legacy four-sidecar topology and its
`BOT_BOTTLE_SIDECAR_BUNDLE` feature flag are gone."""

from __future__ import annotations

import os


# Bundle image. Defaults to a built-locally tag (built from the
# repo's Dockerfile.sidecars via compose `build:`). Operators
# pinning to a published digest can override via env.
SIDECAR_BUNDLE_IMAGE = os.environ.get(
    "BOT_BOTTLE_SIDECAR_IMAGE",
    "bot-bottle-sidecars:latest",
)

SIDECAR_BUNDLE_DOCKERFILE = "Dockerfile.sidecars"


def sidecar_bundle_container_name(slug: str) -> str:
    """`bot-bottle-sidecars-<slug>`. Same prefix scheme as the
    per-sidecar containers it replaces, so the dashboard's
    discovery-by-prefix logic keeps working."""
    return f"bot-bottle-sidecars-{slug}"
