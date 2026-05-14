"""Cred-proxy provisioning inside a running Docker bottle (PRD 0010).

Writes the agent-side configuration that points each tool at the
per-bottle cred-proxy sidecar:

  - ~/.npmrc                      — `registry=` pointing at /npm/
  - ~/.gitconfig (appended)        — `insteadOf` rules for the
                                     github / gitea hosts the bottle
                                     declared a token for
  - ~/.config/tea/config.yml       — per-gitea login pointing at
                                     /gitea/<host>/

The ANTHROPIC_BASE_URL env var is set at `docker run -e` time by the
backend's launch step, not here — it has to be in the agent's environ
before claude starts, and there is no point in writing it to a dotfile
the agent would have to source. See `prepare.py` for that.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ....cred_proxy import CredProxyUpstream
from ....log import info
from .. import util as docker_mod
from ..bottle_plan import DockerBottlePlan
from ..cred_proxy import cred_proxy_url


def provision_cred_proxy(plan: DockerBottlePlan, target: str) -> None:
    """Drop the agent-side dotfiles for each declared cred-proxy
    route. No-op when the bottle has no tokens."""
    upstreams = plan.cred_proxy_plan.upstreams
    if not upstreams:
        return
    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    git_gate_hosts = {g.UpstreamHost for g in bottle.git}
    _provision_npmrc(plan, target, upstreams)
    _provision_gitconfig(plan, target, upstreams, git_gate_hosts)
    _provision_tea_config(plan, target, upstreams)


# --- npm --------------------------------------------------------------------


def render_npmrc(upstreams: tuple[CredProxyUpstream, ...]) -> str:
    """Render `~/.npmrc` content. No-op (empty string) when no npm
    route is declared, so callers can branch on emptiness.

    The proxy strips inbound Authorization and injects its own — the
    npmrc deliberately carries no `_authToken`. The registry alone
    is enough."""
    for u in upstreams:
        if u.kind == "npm":
            return f"registry={cred_proxy_url()}{u.path}\n"
    return ""


def _provision_npmrc(
    plan: DockerBottlePlan,
    target: str,
    upstreams: tuple[CredProxyUpstream, ...],
) -> None:
    content = render_npmrc(upstreams)
    if not content:
        return
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_npmrc = f"{container_home}/.npmrc"
    npmrc = plan.stage_dir / "agent_npmrc"
    npmrc.write_text(content)
    npmrc.chmod(0o600)
    info(f"writing {container_npmrc} (cred-proxy npm registry)")
    subprocess.run(
        ["docker", "cp", str(npmrc), f"{target}:{container_npmrc}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    docker_mod.docker_exec_root(target, ["chown", "node:node", container_npmrc])
    docker_mod.docker_exec_root(target, ["chmod", "644", container_npmrc])


# --- git config -------------------------------------------------------------


def render_cred_proxy_gitconfig(
    upstreams: tuple[CredProxyUpstream, ...],
    git_gate_hosts: set[str] = frozenset(),  # type: ignore[assignment]
) -> str:
    """Render the `~/.gitconfig` fragment for cred-proxy insteadOf
    rewrites. Empty string when no github / gitea routes are declared.

    The rewrite is suppressed for any host that's also declared in
    `bottle.git`. git-gate is the canonical git path on those hosts —
    its pre-receive runs gitleaks before forwarding the push. A
    cred-proxy https://<host>/ rewrite would route HTTPS git ops
    around the gate. cred-proxy still refuses smart-HTTP push at
    runtime (defense in depth), but suppressing the rewrite means
    `git clone https://<host>/...` doesn't have a tempting shortcut
    that just confuses on push.

    github expands to one rewrite (https://github.com/... → /gh-git/...,
    the git transport endpoint); /gh-api/ stays unmapped here because
    tools call api.github.com directly rather than through git.
    Gitea entries get one rewrite per declared host."""
    rules: list[str] = []
    for u in upstreams:
        if u.kind == "github" and u.path == "/gh-git/":
            if "github.com" in git_gate_hosts:
                continue
            rules.append(
                f'[url "{cred_proxy_url()}/gh-git/"]\n'
                f"\tinsteadOf = https://github.com/\n"
            )
        elif u.kind == "gitea":
            # u.path is /gitea/<host>/; derive the host the same way
            # the route table did so we match git_gate's UpstreamHost.
            host = u.path[len("/gitea/"):].rstrip("/")
            if host in git_gate_hosts:
                continue
            rules.append(
                f'[url "{cred_proxy_url()}{u.path}"]\n'
                f"\tinsteadOf = {u.upstream}/\n"
            )
    if not rules:
        return ""
    return (
        "# claude-bottle cred-proxy (PRD 0010): rewrite https://<host>/ to\n"
        "# the per-bottle cred-proxy sidecar, which holds the upstream\n"
        "# credential and injects the Authorization header.\n"
        + "".join(rules)
    )


def _provision_gitconfig(
    plan: DockerBottlePlan,
    target: str,
    upstreams: tuple[CredProxyUpstream, ...],
    git_gate_hosts: set[str],
) -> None:
    """Append the cred-proxy insteadOf rules to ~/.gitconfig. Runs
    after `provision_git`, so any git-gate rules already live in the
    file; we append rather than overwrite. Hosts already brokered by
    git-gate are skipped — git-gate is the canonical git path there."""
    content = render_cred_proxy_gitconfig(upstreams, git_gate_hosts)
    if not content:
        return
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_gitconfig = f"{container_home}/.gitconfig"
    info(f"appending cred-proxy insteadOf rules to {container_gitconfig}")
    # Use `tee -a` over stdin so the content never lands on argv and the
    # append is atomic from the agent's perspective. `tee` runs as the
    # node user (the default in the container) so ownership is preserved.
    result = subprocess.run(
        ["docker", "exec", "-i", target, "tee", "-a", container_gitconfig],
        input=content,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        # Fall back to root-tee in case ~/.gitconfig didn't exist as the
        # node user yet (no git-gate rules were written). The chown
        # below makes ownership consistent.
        result_root = subprocess.run(
            ["docker", "exec", "-i", "-u", "0", target,
             "tee", "-a", container_gitconfig],
            input=content,
            text=True,
            capture_output=True,
            check=True,
        )
        _ = result_root  # silence unused
        docker_mod.docker_exec_root(target, ["chown", "node:node", container_gitconfig])
        docker_mod.docker_exec_root(target, ["chmod", "644", container_gitconfig])


# --- tea --------------------------------------------------------------------


def render_tea_config(upstreams: tuple[CredProxyUpstream, ...]) -> str:
    """Render `~/.config/tea/config.yml`. One `logins:` entry per
    gitea route, pointing at the cred-proxy. The proxy substitutes
    the real token; the value in `token:` here is a placeholder and
    is replaced by the proxy on every request, but `tea` won't make
    calls without a non-empty token field."""
    giteas = [u for u in upstreams if u.kind == "gitea"]
    if not giteas:
        return ""
    lines = ["logins:"]
    for u in giteas:
        # Derive a stable login name from the host (the part of the
        # path between /gitea/ and the trailing /).
        host = u.path[len("/gitea/"):].rstrip("/")
        lines.extend([
            f"- name: {host}",
            f"  url: {cred_proxy_url()}{u.path}",
            "  token: cred-proxy-placeholder",
            "  default: false",
            "  ssh_host: \"\"",
            "  ssh_key: \"\"",
            "  insecure: false",
        ])
    return "\n".join(lines) + "\n"


def _provision_tea_config(
    plan: DockerBottlePlan,
    target: str,
    upstreams: tuple[CredProxyUpstream, ...],
) -> None:
    content = render_tea_config(upstreams)
    if not content:
        return
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_tea = f"{container_home}/.config/tea/config.yml"
    cfg = plan.stage_dir / "agent_tea_config.yml"
    cfg.write_text(content)
    cfg.chmod(0o600)
    info(f"writing {container_tea} ({len([u for u in upstreams if u.kind == 'gitea'])} gitea login(s))")
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
