"""Host-side egress route-apply for the docker backend.

Live route-apply is not supported: the shared gateway resolves egress policy
per request against the orchestrator rather than reloading a per-bottle routes
file, so a reload request fails closed until a gateway-side apply exists.
"""

from __future__ import annotations

from ..egress_apply import EgressApplicator, EgressApplyError


class DockerEgressApplicator(EgressApplicator):
    def _signal_bundle_reload(self, slug: str) -> None:
        del slug
        raise EgressApplyError(
            "live egress route-apply is not supported: the shared "
            "gateway resolves egress policy per request, and a "
            "gateway-side apply does not exist yet."
        )


applicator = DockerEgressApplicator()


__all__ = ["DockerEgressApplicator", "EgressApplyError", "applicator"]
