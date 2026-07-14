"""Sidecar bundle constants + helpers for the Docker backend
(PRD 0024).

A per-bottle sidecar bundle runs egress + git-gate + supervise as one
container under a small Python init supervisor. As of PRD 0024 chunk 5
the bundle is the only shape — the legacy four-sidecar topology and its
`BOT_BOTTLE_SIDECAR_BUNDLE` feature flag are gone.

The bundle's image is the **same data plane** as the consolidated
per-host gateway (PRD 0070), so it is one image with one name and one
Dockerfile: `bot-bottle-gateway` / `Dockerfile.gateway` (#384). These
constants alias the gateway's so a per-bottle bundle and the shared
gateway can never drift onto different images."""

from __future__ import annotations

from ...orchestrator.gateway import GATEWAY_DOCKERFILE, GATEWAY_IMAGE


# The per-bottle bundle image == the gateway data-plane image (aliased so
# there is one source of truth; the `BOT_BOTTLE_GATEWAY_IMAGE` env override
# on the gateway constant applies here too).
SIDECAR_BUNDLE_IMAGE = GATEWAY_IMAGE
SIDECAR_BUNDLE_DOCKERFILE = GATEWAY_DOCKERFILE


def sidecar_bundle_container_name(slug: str) -> str:
    """`bot-bottle-sidecars-<slug>`. Same prefix scheme as the
    per-sidecar containers it replaces, so the dashboard's
    discovery-by-prefix logic keeps working. (The per-bottle *container*
    name keeps the historical `sidecars-` prefix; only the *image* was
    renamed to `bot-bottle-gateway` in #384.)"""
    return f"bot-bottle-sidecars-{slug}"
