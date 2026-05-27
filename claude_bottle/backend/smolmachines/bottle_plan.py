"""SmolmachinesBottlePlan — concrete BottlePlan for the smolmachines
backend (PRD 0023).

Chunk 1 fields: slug, smolfile_path, gvproxy_config_path, gvproxy
subnet + socket, and the per-bottle port map. VM lifecycle fields
(machine name, OCI archive path, etc.) land in later chunks as the
launch flow grows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...log import info
from .. import BottlePlan


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
        """Compact y/N preflight for the smolmachines path. Mirrors
        the docker preflight's layout so operators don't have to
        learn two formats."""
        del remote_control  # not surfaced in the compact summary
        spec = self.spec
        manifest = spec.manifest
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)

        info(f"backend: smolmachines")
        info(f"agent:   {spec.agent_name}")
        info(f"bottle:  {agent.bottle}")
        info(f"slug:    {self.slug}")
        info(f"gvproxy: {self.gvproxy_gateway} on {self.gvproxy_subnet}")

        env_names = sorted(bottle.env.keys())
        skills = list(agent.skills)
        upstreams = [g.Name for g in bottle.git]
        routes = [r.host for r in bottle.egress.routes]
        info(f"env:     {', '.join(env_names) if env_names else '(none)'}")
        info(f"skills:  {', '.join(skills) if skills else '(none)'}")
        info(f"git:     {', '.join(upstreams) if upstreams else '(none)'}")
        info(f"routes:  {', '.join(routes) if routes else '(none)'}")
        info(f"smolfile:       {self.smolfile_path}")
        info(f"gvproxy config: {self.gvproxy_config_path}")
        info(
            "(chunk 1 of PRD 0023: prepare-only — launch is "
            "not yet implemented)"
        )
