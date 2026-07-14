"""Host-side egress route-apply for the macos-container backend.

The per-bottle companion container this used to signal (`container kill
--signal HUP <container>`) was removed in the de-sidecar cleanup (#385),
along with the disabled macOS launch path. Fails closed until the macOS
backend grows the consolidated gateway.
"""

from __future__ import annotations

from ..egress_apply import EgressApplicator, EgressApplyError


class MacOSContainerEgressApplicator(EgressApplicator):
    def _signal_bundle_reload(self, slug: str) -> None:
        del slug
        raise EgressApplyError(
            "live egress route-apply was removed with the per-bottle "
            "companion container (#385); the macos-container backend is "
            "disabled until it uses the consolidated gateway."
        )


applicator = MacOSContainerEgressApplicator()


__all__ = ["MacOSContainerEgressApplicator", "EgressApplyError", "applicator"]
