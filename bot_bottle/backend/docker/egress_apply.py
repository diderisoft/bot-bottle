"""Host-side helper for egress sidecar inspection and live updates.

The approve path uses this module to validate a proposed routes file,
write it to the bottle's live egress state dir, and signal the sidecar
bundle so the mitmproxy addon reloads it.
"""

from __future__ import annotations

import os
import subprocess

from ...egress import EGRESS_ROUTES_IN_CONTAINER
from ...log import warn
from ..egress_apply import EgressApplicator, EgressApplyError
from .sidecar_bundle import sidecar_bundle_container_name


def fetch_current_routes(slug: str) -> str:
    container = sidecar_bundle_container_name(slug)
    r = subprocess.run(
        ["docker", "exec", container, "cat", EGRESS_ROUTES_IN_CONTAINER],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise EgressApplyError(
            f"could not read routes.yaml from {container}: "
            f"{(r.stderr or '').strip() or 'container not running?'}"
        )
    return r.stdout


class DockerEgressApplicator(EgressApplicator):
    def _signal_bundle_reload(self, slug: str) -> None:
        container = sidecar_bundle_container_name(slug)
        result = subprocess.run(
            ["docker", "kill", "--signal", "HUP", container],
            capture_output=True, text=True, check=False, env=os.environ,
        )
        if result.returncode != 0:
            last_error = (result.stderr or "").strip() or (result.stdout or "").strip()
            warn(
                f"egress: routes updated on disk for {slug}, but bundle reload failed: "
                f"{last_error or 'docker kill failed'}"
            )
            raise EgressApplyError(
                f"could not reload egress bundle {container}: "
                f"{last_error or 'docker kill failed'}"
            )


_applicator = DockerEgressApplicator()


def apply_routes_change(slug: str, content: str) -> tuple[str, str]:
    return _applicator.apply_routes_change(slug, content)


validate_routes_content = EgressApplicator.validate_routes_content


__all__ = [
    "DockerEgressApplicator",
    "EgressApplyError",
    "apply_routes_change",
    "fetch_current_routes",
    "validate_routes_content",
]
