"""DockerBottlePlan — concrete subclass of BottlePlan.

Carries the Docker-specific resolved fields produced by
DockerBottleBackend.prepare. The launch step consumes it without
further resolution; show_plan-style rendering is the `print` method.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from ...agent_provider import PromptMode
from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...log import info
from ...pipelock import PipelockProxyPlan
from ...supervise import SupervisePlan
from .. import BottlePlan
from ..print_util import print_multi, visible_agent_env_names


@dataclass(frozen=True)
class DockerBottlePlan(BottlePlan):
    """Docker-specific resolved fields produced by
    DockerBottleBackend.prepare. Inherits `spec` and `stage_dir` from
    BottlePlan."""

    slug: str
    container_name: str
    container_name_pinned: bool
    image: str
    derived_image: str           # "" -> no derived image
    runtime_image: str           # image actually launched (derived or base)
    # Absolute path to the Dockerfile that builds `image`. Empty means
    # use the repo's default Dockerfile. Populated to a per-bottle
    # state file (~/.bot-bottle/state/<slug>/Dockerfile) after a
    # capability-block remediation (PRD 0016).
    dockerfile_path: str
    env_file: Path               # docker --env-file: NAME=VALUE literals
    # name -> value for vars forwarded into the docker-run child process
    # via subprocess env (so values never land on argv or in a file).
    # repr=False keeps secret/interpolated/OAuth values out of any
    # accidental log of the plan dataclass.
    forwarded_env: dict[str, str] = field(repr=False)
    prompt_file: Path
    proxy_plan: PipelockProxyPlan
    git_gate_plan: GitGatePlan
    egress_plan: EgressPlan
    # None when bottle.supervise is False. PRD 0013 supervise sidecar
    # is opt-in via the manifest's bottle.supervise field.
    supervise_plan: SupervisePlan | None
    use_runsc: bool
    agent_command: str = "claude"
    agent_prompt_mode: PromptMode = "append_file"
    agent_provider_template: str = "claude"
    codex_auth_file: Path | None = None

    def print(self, *, remote_control: bool) -> None:
        """Render the y/N preflight summary to stderr — compact form
        intended to fit on screen without scrolling. The full
        structured shape (image, container, runtime, etc.) lives on
        this dataclass for tooling that wants to introspect it."""
        del remote_control  # not surfaced in the compact summary
        spec = self.spec
        manifest = spec.manifest
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)
        # The agent sees the union of literal env names (rendered into
        # --env-file) and forwarded env names (`-e NAME` with the
        # value arriving via subprocess env). The forwarded set holds
        # the OAuth token (CLAUDE_CODE_OAUTH_TOKEN) and any host-env
        # interpolations from the manifest; egress holds
        # upstream tokens in its own environ, so no token forwarding
        # from the agent to the proxy is needed.
        env_names = visible_agent_env_names(
            sorted(set(bottle.env.keys()) | set(self.forwarded_env.keys())),
            agent_provider_template=self.agent_provider_template,
        )

        print(file=sys.stderr)
        info(f"agent           : {spec.agent_name}")
        info(f"provider        : {self.agent_provider_template}")
        print_multi("env             ", env_names)
        print_multi("skills          ", list(agent.skills))
        info(f"bottle          : {agent.bottle}")

        identity = manifest.git_identity_summary(spec.agent_name)
        if identity:
            info(f"  git identity  : {identity}")

        git_lines = [
            f"{u.upstream_host}:{u.upstream_port}"
            for u in self.git_gate_plan.upstreams
        ]
        if git_lines:
            print_multi("  git gate      ", git_lines)

        if self.egress_plan.routes:
            egress_lines = []
            for r in self.egress_plan.routes:
                auth = f" [auth:{r.auth_scheme}]" if r.auth_scheme else ""
                egress_lines.append(f"{r.host}{auth}")
            print_multi("  egress        ", egress_lines)
        print(file=sys.stderr)
