"""smolmachines `_resolve_plan` (PRD 0023 chunk 1).

Lays down the two config files the launch step will consume —
Smolfile (TOML) and gvproxy YAML — under the bottle's stage dir.
No VM bringup. The plan it returns is enough for the y/N
preflight to render."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ...backend import BottleSpec
from ...backend.docker.bottle_state import (
    BottleMetadata,
    bottle_identity,
    write_metadata,
)
from ...backend.docker.sidecar_bundle import sidecar_bundle_container_name
from .bottle_plan import SmolmachinesBottlePlan
from .gvproxy_config import (
    PortForward,
    gvproxy_config_build,
    gvproxy_config_write,
)
from .smolfile import (
    GVPROXY_GIT_GATE_GATEWAY_PORT,
    GVPROXY_PIPELOCK_GATEWAY_PORT,
    GVPROXY_SUPERVISE_GATEWAY_PORT,
    smolfile_build,
    smolfile_write,
)
from .util import (
    allocate_loopback_port,
    smolmachines_gvproxy_subnet,
    smolmachines_preflight,
)


def resolve_plan(
    spec: BottleSpec, *, stage_dir: Path
) -> SmolmachinesBottlePlan:
    """Materialize the smolmachines plan. Three things land on disk
    under stage_dir:

      - `gvproxy.sock` path (created at launch time by gvproxy,
        not by prepare — prepare only records where it'll go).
      - `gvproxy.yaml` — subnet + DNS rule + port_forwards.
      - `smolfile.toml` — guest command/env + virtio-net device
        wired to the gvproxy unixgram socket.

    The y/N preflight reads from the returned plan; chunk-2 launch
    consumes the file paths it points at."""
    smolmachines_preflight()

    manifest = spec.manifest
    agent = manifest.agents[spec.agent_name]
    bottle = manifest.bottle_for(spec.agent_name)

    slug = spec.identity or bottle_identity(spec.agent_name)

    # Record minimal metadata. `resume` (PRD 0016) reuses the slug
    # via spec.identity, so the metadata file is the recoverability
    # contract — keep it in lockstep with the docker backend's
    # bottle_state schema.
    write_metadata(BottleMetadata(
        identity=slug,
        agent_name=spec.agent_name,
        cwd=spec.user_cwd if spec.copy_cwd else "",
        copy_cwd=spec.copy_cwd,
        started_at=datetime.now(timezone.utc).isoformat(),
        # No compose project for smolmachines bottles. Empty string
        # so the dashboard's compose-project-based discovery skips
        # these entries cleanly until chunk 4 teaches it about
        # `smolvm machine list`.
        compose_project="",
    ))

    # Per-bottle gvproxy subnet + gateway. Deterministic from slug;
    # collisions surface at launch time (chunk 2).
    subnet, gateway = smolmachines_gvproxy_subnet(slug)

    # Allocate one host-side loopback port per active sidecar
    # daemon. The bundle (PRD 0024) binds these; gvproxy
    # port-forwards from a fixed gateway port -> host port.
    host_port_map: dict[str, int] = {
        "pipelock": allocate_loopback_port(),
    }
    port_forwards: list[PortForward] = [PortForward(
        gateway_port=GVPROXY_PIPELOCK_GATEWAY_PORT,
        host_port=host_port_map["pipelock"],
    )]
    if bottle.git:
        host_port_map["git-gate"] = allocate_loopback_port()
        port_forwards.append(PortForward(
            gateway_port=GVPROXY_GIT_GATE_GATEWAY_PORT,
            host_port=host_port_map["git-gate"],
        ))
    if bottle.supervise:
        host_port_map["supervise"] = allocate_loopback_port()
        port_forwards.append(PortForward(
            gateway_port=GVPROXY_SUPERVISE_GATEWAY_PORT,
            host_port=host_port_map["supervise"],
        ))

    # Render + write the two config files.
    gvproxy_socket = stage_dir / "gvproxy.sock"
    gvproxy_yaml = stage_dir / "gvproxy.yaml"
    smolfile = stage_dir / "smolfile.toml"

    gvproxy_config_write(
        gvproxy_config_build(
            subnet=subnet,
            gateway=gateway,
            port_forwards=tuple(port_forwards),
        ),
        gvproxy_yaml,
    )

    # Build the guest env. proxy_url points at the gvproxy gateway;
    # gvproxy resolves `proxy.internal` to the gateway IP via its
    # DNS rule, then port-forwards to the host sidecar bundle.
    guest_env: dict[str, str] = {
        **bottle.env,
        "HTTPS_PROXY": f"http://proxy.internal:{GVPROXY_PIPELOCK_GATEWAY_PORT}",
        "HTTP_PROXY":  f"http://proxy.internal:{GVPROXY_PIPELOCK_GATEWAY_PORT}",
        "NO_PROXY":    "localhost,127.0.0.1",
    }
    if bottle.git:
        guest_env["GIT_GATE_URL"] = (
            f"git://proxy.internal:{GVPROXY_GIT_GATE_GATEWAY_PORT}"
        )
    if bottle.supervise:
        guest_env["MCP_SUPERVISE_URL"] = (
            f"http://proxy.internal:{GVPROXY_SUPERVISE_GATEWAY_PORT}"
        )
    smolfile_write(
        smolfile_build(
            slug=slug,
            gvproxy_socket=gvproxy_socket,
            env=guest_env,
        ),
        smolfile,
    )

    return SmolmachinesBottlePlan(
        spec=spec,
        stage_dir=stage_dir,
        slug=slug,
        smolfile_path=smolfile,
        gvproxy_config_path=gvproxy_yaml,
        gvproxy_socket=gvproxy_socket,
        gvproxy_subnet=subnet,
        gvproxy_gateway=gateway,
        host_port_map=host_port_map,
    )


# Used by future cleanup logic — the bundle container that runs on
# the host carries this name even when its host is a smolmachines
# microVM. Re-exported here so chunk 3's sidecar-bringup path has
# a single import target.
__all__ = [
    "resolve_plan",
    "sidecar_bundle_container_name",
]
