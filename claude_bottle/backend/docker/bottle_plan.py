"""DockerBottlePlan — concrete subclass of BottlePlan.

Carries the Docker-specific resolved fields produced by
DockerBottleBackend.prepare. The launch step consumes it without
further resolution; show_plan-style rendering is the `print` method.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from ...log import info
from ...manifest import Agent, Bottle
from ...pipelock import PipelockProxyPlan, pipelock_effective_allowlist
from .. import BottlePlan


@dataclass(frozen=True)
class _PlanView:
    """Fields that both `print` and `to_dict` need but the plan
    doesn't store directly. Cheap to compute; computed once per call."""

    agent: Agent
    bottle: Bottle
    env_names: list[str]
    ssh_hosts: list[str]
    prompt_first_line: str


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
    env_file: Path               # docker --env-file: NAME=VALUE literals
    # name -> value for vars forwarded into the docker-run child process
    # via subprocess env (so values never land on argv or in a file).
    # repr=False keeps secret/interpolated/OAuth values out of any
    # accidental log of the plan dataclass.
    forwarded_env: dict[str, str] = field(repr=False)
    prompt_file: Path
    proxy_plan: PipelockProxyPlan
    allowlist_summary: str
    use_runsc: bool

    def _view(self) -> _PlanView:
        spec = self.spec
        manifest = spec.manifest
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)
        env_names = list(bottle.env.keys())
        if spec.forward_oauth_token:
            env_names.append("CLAUDE_CODE_OAUTH_TOKEN")
        return _PlanView(
            agent=agent,
            bottle=bottle,
            env_names=env_names,
            ssh_hosts=[e.Host for e in bottle.ssh],
            prompt_first_line=agent.prompt.splitlines()[0] if agent.prompt else "",
        )

    def print(self, *, remote_control: bool) -> None:
        """Render the y/N preflight summary to stderr. Pure presentation."""
        v = self._view()
        spec = self.spec
        runtime_label = "runsc (gVisor)" if self.use_runsc else "runc (default)"

        print(file=sys.stderr)
        info(f"agent           : {spec.agent_name}")
        info(f"image           : {self.image}")
        if self.derived_image:
            info(
                f"cwd             : {spec.user_cwd} -> /home/node/workspace "
                f"(derived: {self.derived_image})"
            )
        info(f"container       : {self.container_name}")
        info(f"stage dir       : {self.stage_dir}")
        info("env (names only): " + (", ".join(v.env_names) if v.env_names else "(none)"))
        info("skills          : " + (" ".join(v.agent.skills) if v.agent.skills else "(none)"))
        info(f"docker runtime  : {runtime_label}")
        info(f"bottle             : {v.agent.bottle}")
        if v.ssh_hosts:
            info(f"  ssh hosts     : {', '.join(v.ssh_hosts)}")
        else:
            info("  ssh hosts     : (none)")
        info(f"  egress        : {self.allowlist_summary}")
        info(
            f"prompt          : {len(v.agent.prompt)} chars; "
            f"first line: {v.prompt_first_line or '(empty)'}"
        )
        info("remote-control  : " + ("enabled" if remote_control else "disabled"))
        print(file=sys.stderr)

    def to_dict(self, *, remote_control: bool) -> dict[str, object]:
        v = self._view()
        hosts = pipelock_effective_allowlist(v.bottle)
        return {
            "agent": self.spec.agent_name,
            "bottle": v.agent.bottle,
            "container_name": self.container_name,
            "image": self.image,
            "derived_image": self.derived_image,
            "stage_dir": str(self.stage_dir),
            "runtime": "runsc" if self.use_runsc else "runc",
            "env_names": v.env_names,
            "skills": list(v.agent.skills),
            "ssh_hosts": v.ssh_hosts,
            "egress": {
                "host_count": len(hosts),
                "hosts": hosts,
            },
            "prompt": {
                "length": len(v.agent.prompt),
                "first_line": v.prompt_first_line,
            },
            "remote_control": remote_control,
        }
