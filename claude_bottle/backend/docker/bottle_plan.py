"""DockerBottlePlan — concrete subclass of BottlePlan.

Carries the Docker-specific resolved fields produced by
DockerBottleBackend.prepare. The launch step consumes it without
further resolution; show_plan-style rendering is the `print` method.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from ...egress_proxy import EgressProxyPlan
from ...git_gate import GitGatePlan
from ...log import info
from ...pipelock import PipelockProxyPlan
from ...supervise import SupervisePlan
from .. import BottlePlan


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
    # state file (~/.claude-bottle/state/<slug>/Dockerfile) after a
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
    egress_proxy_plan: EgressProxyPlan
    # None when bottle.supervise is False. PRD 0013 supervise sidecar
    # is opt-in via the manifest's bottle.supervise field.
    supervise_plan: SupervisePlan | None
    allowlist_summary: str
    use_runsc: bool

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
        # interpolations from the manifest; egress-proxy holds
        # upstream tokens in its own environ, so no token forwarding
        # from the agent to the proxy is needed.
        env_names = sorted(set(bottle.env.keys()) | set(self.forwarded_env.keys()))

        def _multi(label: str, values: list[str]) -> None:
            """Print a label with N continuation-indented values. Used
            for env / skills / git-gate / egress-proxy where one item
            per line keeps the summary scannable."""
            if not values:
                info(f"{label}: (none)")
                return
            info(f"{label}: {values[0]}")
            indent = " " * (len(label) + 2)
            for v_ in values[1:]:
                info(f"{indent}{v_}")

        print(file=sys.stderr)
        info(f"agent           : {spec.agent_name}")
        _multi("env             ", env_names)
        _multi("skills          ", list(agent.skills))
        info(f"bottle          : {agent.bottle}")

        git_lines = [
            f"{u.upstream_host}:{u.upstream_port}"
            for u in self.git_gate_plan.upstreams
        ]
        if git_lines:
            _multi("  git gate      ", git_lines)

        if self.egress_proxy_plan.routes:
            egress_lines = []
            for r in self.egress_proxy_plan.routes:
                auth = f" [auth:{r.auth_scheme}]" if r.auth_scheme else ""
                egress_lines.append(f"{r.host}{auth}")
            _multi("  egress-proxy  ", egress_lines)
        print(file=sys.stderr)

