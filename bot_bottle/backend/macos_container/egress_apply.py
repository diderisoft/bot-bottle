"""Host-side egress apply for the macos-container backend.

Uses `container kill --signal HUP` (Apple Container framework) instead
of `docker kill` to signal the sidecar bundle.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...bottle_state import egress_state_dir
from ...egress import EGRESS_ROUTES_FILENAME
from ...log import warn
from ..docker.egress_apply import EgressApplyError, validate_routes_content
from .launch import sidecar_container_name


def apply_routes_change(slug: str, content: str) -> tuple[str, str]:
    """Persist `content` to the live routes file and reload egress."""
    validate_routes_content(content)
    routes_path = _routes_path(slug)
    routes_path.parent.mkdir(parents=True, exist_ok=True)
    before = routes_path.read_text(encoding="utf-8") if routes_path.exists() else ""
    routes_path.write_text(content, encoding="utf-8")
    routes_path.chmod(0o600)
    _signal_bundle_reload(slug)
    return before, content


def _routes_path(slug: str) -> Path:
    return egress_state_dir(slug) / EGRESS_ROUTES_FILENAME


def _signal_bundle_reload(slug: str) -> None:
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


__all__ = ["apply_routes_change"]
