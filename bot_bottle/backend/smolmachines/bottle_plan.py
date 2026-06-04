"""SmolmachinesBottlePlan — concrete BottlePlan for the smolmachines
backend (PRD 0023).

Slug + bundle docker subnet / gateway / pinned IP + smolvm
machine name + agent `.smolmachine` artifact + per-bottle guest
env. Provisioning fields (CA cert path, prompt path, etc.) land
in chunk 4."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...agent_provider import PromptMode
from .. import BottlePlan


@dataclass(frozen=True)
class SmolmachinesBottlePlan(BottlePlan):
    """Resolved fields the launch step needs to bring up the bottle.

    Inherits `spec`, `stage_dir`, `git_gate_plan`, `egress_plan`,
    `supervise_plan`, and `agent_provision` from BottlePlan."""

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
    # since bot-bottle-claude:latest lives in the operator's local
    # docker daemon and smolvm's crane backend can't read from
    # there; chunk 4 resolves the agent-image-conversion gap
    # (push to a registry first, or smolvm grows a docker-daemon
    # transport).
    machine_name: str
    # Agent image ref (docker tag). `launch` runs the
    # build → save → registry push → smolvm pack pipeline against
    # this and feeds the resulting `.smolmachine` artifact to
    # `machine_create --from`. The pipeline runs at launch time
    # (not prepare time) so the docker build output doesn't garble
    # the dashboard's preflight modal.
    agent_image_ref: str
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
    # Inner Plans for the sidecar bundle daemons. The same shape the
    # docker backend uses — same `.prepare()` calls produced
    # them — but our launch step doesn't populate the
    # docker-specific network fields (internal_network,
    # egress_network) because the smolmachines bundle isn't on
    # docker's `--internal` + egress bridge topology; it's on a
    # per-bottle bridge with a pinned IP. The unused fields stay
    # at their dataclass defaults.
    # Agent-side endpoints. On Docker Desktop the docker bridge
    # IPs aren't reachable from the smolvm guest (TSI uses macOS
    # networking; docker container IPs live in the daemon's VM),
    # so the agent dials the bundle via host loopback +
    # docker-published random ports. Empty at prepare time;
    # launch populates these after bundle bringup via
    # `dataclasses.replace`. Format: a `host:port` for git-gate
    # (insteadOf URL prefix) + full URLs for proxy / supervise.
    agent_proxy_url: str = ""
    agent_git_gate_host: str = ""
    agent_supervise_url: str = ""

    @property
    def agent_command(self) -> str:
        return self.agent_provision.command

    @property
    def agent_prompt_mode(self) -> PromptMode:
        return self.agent_provision.prompt_mode

    @property
    def agent_provider_template(self) -> str:
        return self.agent_provision.template

    @property
    def agent_dockerfile_path(self) -> str:
        return self.agent_provision.dockerfile
