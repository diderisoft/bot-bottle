"""macOS egress apply: unsupported, since the gateway resolves policy per
request. Always fails closed.
"""

from __future__ import annotations

from ..egress_apply import EgressApplicator, EgressApplyError


class MacOSContainerEgressApplicator(EgressApplicator):
    def _signal_bundle_reload(self, slug: str) -> None:
        del slug
        raise EgressApplyError(
            "live egress route-apply is not supported: the shared "
            "gateway resolves egress policy per request, and a "
            "gateway-side apply does not exist yet."
        )


applicator = MacOSContainerEgressApplicator()


__all__ = ["MacOSContainerEgressApplicator", "EgressApplyError", "applicator"]
