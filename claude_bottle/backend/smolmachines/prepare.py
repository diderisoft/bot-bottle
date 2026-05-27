"""smolmachines `_resolve_plan` (PRD 0023 chunk 2a).

Resolves the per-bottle docker subnet + bundle IP and writes the
Smolfile to the stage dir. No VM bringup. The plan it returns is
enough for the y/N preflight to render."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ...backend import BottleSpec
from ...backend.docker.bottle_state import (
    BottleMetadata,
    bottle_identity,
    write_metadata,
)
from .bottle_plan import SmolmachinesBottlePlan
from .smolfile import smolfile_build, smolfile_write
from .util import smolmachines_bundle_subnet, smolmachines_preflight


# Gateway ports the bundle exposes inside its container — pipelock
# HTTPS proxy, git-gate's git-daemon, supervise's MCP. The agent
# inside the smolvm guest dials these on the bundle's pinned IP.
_BUNDLE_PIPELOCK_PORT = 8888
_BUNDLE_GIT_GATE_PORT = 9418
_BUNDLE_SUPERVISE_PORT = 9100


def resolve_plan(
    spec: BottleSpec, *, stage_dir: Path
) -> SmolmachinesBottlePlan:
    """Materialize the smolmachines plan. The Smolfile lands at
    `<stage>/smolfile.toml`; the bundle's docker subnet + pinned
    IP are derived from the slug and carried on the plan for
    launch to consume."""
    smolmachines_preflight()

    manifest = spec.manifest
    bottle = manifest.bottle_for(spec.agent_name)

    slug = spec.identity or bottle_identity(spec.agent_name)

    # Record minimal metadata so `cli.py resume` can recover the
    # slug. Same schema as the docker backend.
    write_metadata(BottleMetadata(
        identity=slug,
        agent_name=spec.agent_name,
        cwd=spec.user_cwd if spec.copy_cwd else "",
        copy_cwd=spec.copy_cwd,
        started_at=datetime.now(timezone.utc).isoformat(),
        # No compose project for smolmachines bottles; chunk 4
        # will give dashboard discovery a backend-specific path.
        compose_project="",
    ))

    subnet, gateway, bundle_ip = smolmachines_bundle_subnet(slug)

    # Agent's env. IP literals; no DNS resolution inside the guest
    # (TSI allowlist contains only `<bundle_ip>/32` — no resolver).
    guest_env: dict[str, str] = {
        **bottle.env,
        "HTTPS_PROXY": f"http://{bundle_ip}:{_BUNDLE_PIPELOCK_PORT}",
        "HTTP_PROXY":  f"http://{bundle_ip}:{_BUNDLE_PIPELOCK_PORT}",
        "NO_PROXY":    "localhost,127.0.0.1",
    }
    if bottle.git:
        guest_env["GIT_GATE_URL"] = (
            f"git://{bundle_ip}:{_BUNDLE_GIT_GATE_PORT}"
        )
    if bottle.supervise:
        guest_env["MCP_SUPERVISE_URL"] = (
            f"http://{bundle_ip}:{_BUNDLE_SUPERVISE_PORT}"
        )

    smolfile_path = stage_dir / "smolfile.toml"
    smolfile_write(
        smolfile_build(env=guest_env, bundle_ip=bundle_ip),
        smolfile_path,
    )

    return SmolmachinesBottlePlan(
        spec=spec,
        stage_dir=stage_dir,
        slug=slug,
        smolfile_path=smolfile_path,
        bundle_subnet=subnet,
        bundle_gateway=gateway,
        bundle_ip=bundle_ip,
    )
