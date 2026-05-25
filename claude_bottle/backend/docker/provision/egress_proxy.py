"""Egress-proxy agent-side provisioning (PRD 0017).

Writes the dotfiles / env-var nudges that point tools needing an
explicit URL config at the canonical upstream. The agent's
`HTTPS_PROXY=egress-proxy` already catches HTTPS traffic — these
provisioners exist for tools that:

  - need an explicit base-URL setting beyond the proxy (claude-code
    via `ANTHROPIC_BASE_URL`), or
  - read a per-tool config to know which upstream to talk to (npm's
    `~/.npmrc`, tea's `~/.config/tea/config.yml`).

The `ANTHROPIC_BASE_URL` env is set at `docker run -e` time by the
backend's launch step, not here — it has to be in the agent's environ
before claude starts, and there is no point in writing it to a
dotfile the agent would have to source. See `prepare.py` for that.
This module handles the rest: ~/.npmrc and ~/.config/tea/config.yml.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ....egress_proxy import EgressProxyRoute
from ....log import info
from .. import util as docker_mod
from ..bottle_plan import DockerBottlePlan


def provision_egress_proxy(plan: DockerBottlePlan, target: str) -> None:
    """Drop the agent-side dotfiles for each declared egress-proxy
    route role. No-op when the bottle has no roles to provision."""
    routes = plan.egress_proxy_plan.routes
    if not routes:
        return
    _provision_npmrc(plan, target, routes)
    _provision_tea_config(plan, target, routes)


# --- npm --------------------------------------------------------------------


def render_npmrc(routes: tuple[EgressProxyRoute, ...]) -> str:
    """Render `~/.npmrc` content. Driven by the `npm-registry` role:
    finds the (single) route that claims it and writes a registry=
    line pointing at the canonical upstream URL. Empty string when
    no such route exists, so callers can branch on emptiness.

    Egress-proxy is on the agent's HTTPS_PROXY, so the canonical
    URL routes through the proxy automatically and gets DLP +
    path_allowlist + auth-injection. The npmrc deliberately carries
    no `_authToken` — the proxy strips inbound Authorization and
    injects the upstream one from the bottle's egress-proxy auth
    config. Manifest validation enforces that the role is a
    singleton, so the first match is the only match."""
    for r in routes:
        if "npm-registry" in r.roles:
            return f"registry=https://{r.host}/\n"
    return ""


def _provision_npmrc(
    plan: DockerBottlePlan,
    target: str,
    routes: tuple[EgressProxyRoute, ...],
) -> None:
    content = render_npmrc(routes)
    if not content:
        return
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_npmrc = f"{container_home}/.npmrc"
    npmrc = plan.stage_dir / "agent_npmrc"
    npmrc.write_text(content)
    npmrc.chmod(0o600)
    info(f"writing {container_npmrc} (egress-proxy npm registry)")
    subprocess.run(
        ["docker", "cp", str(npmrc), f"{target}:{container_npmrc}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    docker_mod.docker_exec_root(target, ["chown", "node:node", container_npmrc])
    docker_mod.docker_exec_root(target, ["chmod", "644", container_npmrc])


# --- tea --------------------------------------------------------------------


def render_tea_config(routes: tuple[EgressProxyRoute, ...]) -> str:
    """Render `~/.config/tea/config.yml`. Driven by the `tea-login`
    role: each route that claims it produces one `logins:` entry
    pointing at the canonical Gitea URL. The egress-proxy injects
    the real token at request time; the value in `token:` here is a
    placeholder. `tea` refuses to make calls without a non-empty
    token field, so the placeholder is necessary."""
    tea_routes = [r for r in routes if "tea-login" in r.roles]
    if not tea_routes:
        return ""
    lines = ["logins:"]
    for r in tea_routes:
        lines.extend([
            f"- name: {r.host}",
            f"  url: https://{r.host}",
            "  token: egress-proxy-placeholder",
            "  default: false",
            "  ssh_host: \"\"",
            "  ssh_key: \"\"",
            "  insecure: false",
        ])
    return "\n".join(lines) + "\n"


def _provision_tea_config(
    plan: DockerBottlePlan,
    target: str,
    routes: tuple[EgressProxyRoute, ...],
) -> None:
    content = render_tea_config(routes)
    if not content:
        return
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_tea = f"{container_home}/.config/tea/config.yml"
    cfg = plan.stage_dir / "agent_tea_config.yml"
    cfg.write_text(content)
    cfg.chmod(0o600)
    info(
        f"writing {container_tea} "
        f"({len([r for r in routes if 'tea-login' in r.roles])} tea login(s))"
    )
    docker_mod.docker_exec_root(
        target, ["mkdir", "-p", str(Path(container_tea).parent)]
    )
    subprocess.run(
        ["docker", "cp", str(cfg), f"{target}:{container_tea}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    docker_mod.docker_exec_root(target, [
        "chown", "-R", "node:node", str(Path(container_tea).parent),
    ])
    docker_mod.docker_exec_root(target, ["chmod", "600", container_tea])
