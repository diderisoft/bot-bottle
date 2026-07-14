"""Host-side egress route-apply for the docker backend.

The per-bottle companion container this used to signal (`docker kill
--signal HUP <container>`) was removed in the de-sidecar cleanup (#385).
In the consolidated model the shared gateway resolves egress policy
per-request against the orchestrator rather than reloading a per-bottle
routes file, so the live per-bottle reload is not supported here and
fails closed until the gateway-side apply lands.
"""

from __future__ import annotations

from ..egress_apply import EgressApplicator, EgressApplyError


class DockerEgressApplicator(EgressApplicator):
    def _signal_bundle_reload(self, slug: str) -> None:
        del slug
        raise EgressApplyError(
            "live egress route-apply was removed with the per-bottle "
            "companion container (#385); route changes will flow through "
            "the consolidated gateway in a follow-up."
        )


applicator = DockerEgressApplicator()


__all__ = ["DockerEgressApplicator", "EgressApplyError", "applicator"]
