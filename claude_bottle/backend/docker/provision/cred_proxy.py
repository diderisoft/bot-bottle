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

from ....cred_proxy import CredProxyRoute
from ....log import info
from .. import util as docker_mod
from ..bottle_plan import DockerBottlePlan
from ..cred_proxy import cred_proxy_url


def provision_cred_proxy(plan: DockerBottlePlan, target: str) -> None:
    """Drop the agent-side dotfiles for each declared cred-proxy
    route. No-op when the bottle has no routes."""
    routes = plan.cred_proxy_plan.routes
    if not routes:
        return
    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    git_gate_hosts = {g.UpstreamHost for g in bottle.git}
    _provision_npmrc(plan, target, routes)
    _provision_gitconfig(plan, target, routes, git_gate_hosts)
    _provision_tea_config(plan, target, routes)


# --- npm --------------------------------------------------------------------


def render_npmrc(routes: tuple[CredProxyRoute, ...]) -> str:
    """Render `~/.npmrc` content. Driven by the `npm-registry` role:
    finds the (single) route that claims it and writes a registry=
    line at the proxy. Empty string when no such route exists, so
    callers can branch on emptiness.

    The proxy strips inbound Authorization and injects its own — the
    npmrc deliberately carries no `_authToken`. The registry alone
    is enough. Manifest validation enforces that the role is a
    singleton, so the first match is the only match."""
    for r in routes:
        if "npm-registry" in r.roles:
            return f"registry={cred_proxy_url()}{r.path}\n"
    return ""


def _provision_npmrc(
    plan: DockerBottlePlan,
    target: str,
    routes: tuple[CredProxyRoute, ...],
) -> None:
    content = render_npmrc(routes)
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
    routes: tuple[CredProxyRoute, ...],
    git_gate_hosts: set[str] = frozenset(),  # type: ignore[assignment]
) -> str:
    """Render the `~/.gitconfig` fragment for cred-proxy insteadOf
    rewrites. Driven by the `git-insteadof` role: each route that
    claims it produces a `[url "<proxy><path>"] insteadOf =
    <upstream>/` block. Empty string when no such route exists.

    The rewrite is suppressed for any route whose upstream host is
    also declared in `bottle.git`. git-gate is the canonical git
    path on those hosts — its pre-receive runs gitleaks before
    forwarding the push. A cred-proxy `https://<host>/` rewrite
    would route HTTPS git ops around the gate. cred-proxy still
    refuses smart-HTTP push at runtime (defense in depth), but
    suppressing the rewrite means `git clone https://<host>/...`
    doesn't have a tempting shortcut that just confuses on push.

    The insteadOf left-hand side comes from `route.upstream` (with a
    trailing `/` so insteadOf matches at the directory boundary),
    so the same renderer handles github.com, gitea.dideric.is, and
    any future host the user wires up."""
    rules: list[str] = []
    for r in routes:
        if "git-insteadof" not in r.roles:
            continue
        # Strip scheme to derive the host for the git-gate overlap
        # check. urllib.parse-free parse: same shape we accept in
        # manifest validation.
        host = r.upstream.removeprefix("https://").partition("/")[0].partition(":")[0]
        if host in git_gate_hosts:
            continue
        rules.append(
            f'[url "{cred_proxy_url()}{r.path}"]\n'
            f"\tinsteadOf = {r.upstream}/\n"
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
    routes: tuple[CredProxyRoute, ...],
    git_gate_hosts: set[str],
) -> None:
    """Append the cred-proxy insteadOf rules to ~/.gitconfig. Runs
    after `provision_git`, so any git-gate rules already live in the
    file; we append rather than overwrite. Hosts already brokered by
    git-gate are skipped — git-gate is the canonical git path there."""
    content = render_cred_proxy_gitconfig(routes, git_gate_hosts)
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


def render_tea_config(routes: tuple[CredProxyRoute, ...]) -> str:
    """Render `~/.config/tea/config.yml`. Driven by the `tea-login`
    role: each route that claims it produces one `logins:` entry
    pointing at the cred-proxy. The proxy substitutes the real
    token at request time; the value in `token:` here is a
    placeholder. `tea` refuses to make calls without a non-empty
    token field, so the placeholder is necessary."""
    tea_routes = [r for r in routes if "tea-login" in r.roles]
    if not tea_routes:
        return ""
    lines = ["logins:"]
    for r in tea_routes:
        # Derive a stable login name from the upstream host. The
        # path may not encode the host (e.g. `/gitea/dideric/` vs
        # upstream gitea.dideric.is), so we read it off `upstream`.
        host = r.upstream.removeprefix("https://").partition("/")[0].partition(":")[0]
        lines.extend([
            f"- name: {host}",
            f"  url: {cred_proxy_url()}{r.path}",
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
    routes: tuple[CredProxyRoute, ...],
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
