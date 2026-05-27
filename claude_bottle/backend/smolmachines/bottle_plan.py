"""SmolmachinesBottlePlan — concrete BottlePlan for the smolmachines
backend (PRD 0023).

Slug + bundle docker subnet / gateway / pinned IP + smolvm
machine name + agent `.smolmachine` artifact + per-bottle guest
env. Provisioning fields (CA cert path, prompt path, etc.) land
in chunk 4."""

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
    # Per-bottle docker subnet for the sidecar bundle container.
    # The bundle runs at `bundle_ip` (always `.2`); the gateway is
    # at `.1`. smolvm's TSI allowlist is set to `bundle_ip/32`.
    bundle_subnet: str
    bundle_gateway: str
    bundle_ip: str
    # smolvm machine name + agent image source. machine_create
    # boots from a packed `.smolmachine` artifact (pre-baked at
    # prepare time via `smolvm pack create`); using `--from`
    # instead of `--image` avoids the registry-pull race we hit
    # when machine_start tried to fetch on-demand and the libkrun
    # agent's network attempt got refused by macOS.
    #
    # Chunk 2d ships with a public placeholder image (alpine)
    # since claude-bottle:latest lives in the operator's local
    # docker daemon and smolvm's crane backend can't read from
    # there; chunk 4 resolves the agent-image-conversion gap
    # (push to a registry first, or smolvm grows a docker-daemon
    # transport).
    machine_name: str
    agent_from_path: Path
    # In-guest env vars (HTTPS_PROXY etc) — IP-literal URLs since
    # the guest has no DNS resolver inside the TSI allowlist.
    # Passed to `smolvm machine create` as `-e K=V` flags.
    # Smolfile-rendering is gone (smolvm 0.8.0's
    # `--smolfile` is mutually exclusive with `--from`, and
    # `--from` is the path that avoids the registry-pull race).
    guest_env: dict[str, str]
    # Path to the agent's prompt file on the host. Always written
    # (mode 0o600) so the in-VM path always exists; the file is
    # empty when the agent has no prompt — claude-code reads it
    # via --append-system-prompt-file only when non-empty.
    prompt_file: Path

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
