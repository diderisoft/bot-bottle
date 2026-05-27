"""SmolmachinesBottlePlan — concrete BottlePlan for the smolmachines
backend (PRD 0023).

Chunk 1 fields: slug, smolfile_path, gvproxy_config_path, gvproxy
subnet + socket, and the per-bottle port map. VM lifecycle fields
(machine name, OCI archive path, etc.) land in later chunks as the
launch flow grows."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from ...log import info
from .. import BottlePlan
from ..print_util import print_multi


@dataclass(frozen=True)
class SmolmachinesBottlePlan(BottlePlan):
    """Resolved fields the launch step needs to bring up the bottle.

    Inherits `spec` and `stage_dir` from BottlePlan."""

    slug: str
    smolfile_path: Path
    gvproxy_config_path: Path
    gvproxy_socket: Path
    gvproxy_subnet: str
    gvproxy_gateway: str
    # Daemon name → host-side loopback port the bundle binds.
    # Always includes "pipelock"; "git-gate" and "supervise"
    # conditional on the bottle's manifest.
    host_port_map: dict[str, int]

    def print(self, *, remote_control: bool) -> None:
        """Compact y/N preflight. Same shape as the Docker
        backend's so operators see one format across backends."""
        del remote_control  # not surfaced in the compact summary
        spec = self.spec
        manifest = spec.manifest
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)

        env_names = sorted(bottle.env.keys())
        upstreams = [
            f"{g.Name} → {g.Upstream}" for g in bottle.git
        ]
        routes = [r.host for r in bottle.egress.routes]

        print(file=sys.stderr)
        info(f"agent           : {spec.agent_name}")
        print_multi("env             ", env_names)
        print_multi("skills          ", list(agent.skills))
        info(f"bottle          : {agent.bottle}")
        if upstreams:
            print_multi("  git gate      ", upstreams)
        if routes:
            print_multi("  egress        ", routes)
        print(file=sys.stderr)
