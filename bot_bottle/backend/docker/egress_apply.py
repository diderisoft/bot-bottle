"""Host-side helper for egress sidecar inspection and live updates.

The approve path uses this module to validate a proposed routes file,
write it to the bottle's live egress state dir, and signal the sidecar
bundle so the mitmproxy addon reloads it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...bottle_state import egress_state_dir, read_metadata
from ...egress import EGRESS_ROUTES_FILENAME, EGRESS_ROUTES_IN_CONTAINER
from ...egress_addon_core import load_routes
from ...log import warn
from .sidecar_bundle import sidecar_bundle_container_name


class EgressApplyError(RuntimeError):
    pass


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


def validate_routes_content(content: str) -> None:
    try:
        load_routes(content)
    except ValueError as e:
        raise EgressApplyError(
            f"proposed routes.yaml is not valid: {e}"
        ) from e


def _routes_path(slug: str) -> Path:
    state_dir = egress_state_dir(slug)
    routes_path = state_dir / EGRESS_ROUTES_FILENAME
    legacy_path = state_dir / "egress_routes.yaml"
    if legacy_path.exists() and not routes_path.exists():
        return legacy_path
    return routes_path


def _signal_bundle_reload(slug: str) -> None:
    container = sidecar_bundle_container_name(slug)
    backend = ""
    metadata = read_metadata(slug)
    if metadata is not None:
        backend = metadata.backend

    candidates: list[list[str]]
    if backend == "macos-container":
        candidates = [["container", "kill", "--signal", "HUP", container]]
    elif backend:
        candidates = [["docker", "kill", "--signal", "HUP", container]]
    else:
        candidates = [
            ["docker", "kill", "--signal", "HUP", container],
            ["container", "kill", "--signal", "HUP", container],
        ]

    last_error = ""
    for argv in candidates:
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                env=os.environ,
            )
        except FileNotFoundError as e:
            last_error = str(e)
            continue
        if result.returncode == 0:
            return
        last_error = (result.stderr or "").strip() or (result.stdout or "").strip()
    warn(
        f"egress: routes updated on disk for {slug}, but bundle reload failed: "
        f"{last_error or 'no reload command succeeded'}"
    )
    raise EgressApplyError(
        f"could not reload egress bundle {container}: "
        f"{last_error or 'no reload command succeeded'}"
    )


__all__ = [
    "EgressApplyError",
    "apply_routes_change",
    "fetch_current_routes",
    "validate_routes_content",
]
