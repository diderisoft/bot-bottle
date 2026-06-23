"""Host-side egress apply for the macos-container backend.

Uses `container kill --signal HUP` (Apple Container framework) instead
of `docker kill` to signal the sidecar bundle.
"""

from __future__ import annotations

import os
import subprocess

from ...log import warn
from ..egress_apply import EgressApplicator, EgressApplyError
from .launch import sidecar_container_name


class MacOSContainerEgressApplicator(EgressApplicator):
    def _signal_bundle_reload(self, slug: str) -> None:
        container = sidecar_container_name(slug)
        result = subprocess.run(
            ["container", "kill", "--signal", "HUP", container],
            capture_output=True, text=True, check=False, env=os.environ,
        )
        if result.returncode != 0:
            last_error = (result.stderr or "").strip() or (result.stdout or "").strip()
            warn(
                f"egress: routes updated on disk for {slug}, but bundle reload failed: "
                f"{last_error or 'container kill failed'}"
            )
            raise EgressApplyError(
                f"could not reload egress bundle {container}: "
                f"{last_error or 'container kill failed'}"
            )


applicator = MacOSContainerEgressApplicator()


__all__ = ["MacOSContainerEgressApplicator", "EgressApplyError", "applicator"]
