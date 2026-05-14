"""DockerBottlePlan — concrete subclass of BottlePlan.

Carries the Docker-specific resolved fields produced by
DockerBottleBackend.prepare. The launch step consumes it without
further resolution; show_plan-style rendering is the `print` method.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from ...cred_proxy import CredProxyPlan
from ...git_gate import GitGatePlan
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
    git_names: list[str]
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
    git_gate_plan: GitGatePlan
    cred_proxy_plan: CredProxyPlan
    allowlist_summary: str
    use_runsc: bool

    def _view(self) -> _PlanView:
        spec = self.spec
        manifest = spec.manifest
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)
        # The agent sees the union of literal env names (rendered into
        # --env-file) and forwarded env names (`-e NAME` with the value
        # arriving via subprocess env). The forwarded set already
        # reflects PRD 0010's switch — when cred-proxy holds the
        # anthropic token, CLAUDE_CODE_OAUTH_TOKEN is absent and
        # ANTHROPIC_BASE_URL is present.
        env_names = sorted(set(bottle.env.keys()) | set(self.forwarded_env.keys()))
        return _PlanView(
            agent=agent,
            bottle=bottle,
            env_names=env_names,
            git_names=[e.Name for e in bottle.git],
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
        if v.git_names:
            info(f"  git remotes   : {', '.join(v.git_names)}")
            git_lines = [
                f"{u.name} -> {u.upstream_host}:{u.upstream_port} "
                f"(gitleaks-scanned)"
                for u in self.git_gate_plan.upstreams
            ]
            info(f"  git gate      : {'; '.join(git_lines)}")
        else:
            info("  git remotes   : (none)")
        if self.cred_proxy_plan.upstreams:
            routes = [f"{u.path}→{u.upstream}" for u in self.cred_proxy_plan.upstreams]
            refs = sorted({u.token_ref for u in self.cred_proxy_plan.upstreams})
            info(f"  cred-proxy    : {len(routes)} route(s); tokens: {', '.join(refs)}")
            for line in routes:
                info(f"                  {line}")
        else:
            info("  cred-proxy    : (none)")
        info(f"  egress        : {self.allowlist_summary}")
        info("  tls intercept : pipelock (per-bottle ephemeral CA, generated at launch)")
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
            "git_remotes": v.git_names,
            "git_gate": [
                {
                    "name": u.name,
                    "upstream": f"{u.upstream_host}:{u.upstream_port}",
                    "upstream_url": u.upstream_url,
                    "known_host_key_pinned": bool(u.known_host_key),
                }
                for u in self.git_gate_plan.upstreams
            ],
            "cred_proxy": [
                {
                    "path": u.path,
                    "upstream": u.upstream,
                    "auth_scheme": u.auth_scheme,
                    "token_ref": u.token_ref,
                    "roles": list(u.roles),
                }
                for u in self.cred_proxy_plan.upstreams
            ],
            "egress": {
                "host_count": len(hosts),
                "hosts": hosts,
                # PRD 0006: pipelock's `tls_interception` block is on
                # for every launched bottle. ca_fingerprint is always
                # null at dry-run because the CA doesn't exist yet —
                # real launches print the fingerprint to stderr from
                # provision_ca. Reserved field for forward-compat.
                "tls_interception": {
                    "enabled": True,
                    "ca_fingerprint": None,
                },
            },
            "prompt": {
                "length": len(v.agent.prompt),
                "first_line": v.prompt_first_line,
            },
            "remote_control": remote_control,
        }
