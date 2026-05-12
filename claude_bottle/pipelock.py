"""Pipelock sidecar lifecycle for the per-agent egress topology.

Pipelock (https://github.com/luckyPipewrench/pipelock) is an HTTP
forward proxy with hostname allowlisting + DLP scanning + URL-entropy
checks. One sidecar per agent, attached to the agent's --internal
network and a per-agent user-defined egress bridge. Combined with
HTTPS_PROXY/HTTP_PROXY pointing at the sidecar's service name, pipelock
is the only egress route the agent has.

Image pin: ghcr.io/luckypipewrench/pipelock@sha256:<digest> for tag 2.3.0.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .manifest import Bottle
from .util import is_ipv4_literal

# Baked-in default allowlist for hosts Claude Code itself needs.
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "api.anthropic.com",
    "statsig.anthropic.com",
    "sentry.io",
    "claude.ai",
    "platform.claude.com",
    "downloads.claude.ai",
    "raw.githubusercontent.com",
)


# --- Allowlist resolution --------------------------------------------------


def pipelock_bottle_allowlist(bottle: Bottle) -> list[str]:
    """Hostnames in bottle.egress.allowlist."""
    return list(bottle.egress.allowlist)


def pipelock_bottle_ssh_hostnames(bottle: Bottle) -> list[str]:
    return [e.Hostname for e in bottle.ssh if e.Hostname]


def pipelock_bottle_ssh_trusted_domains(bottle: Bottle) -> list[str]:
    return [h for h in pipelock_bottle_ssh_hostnames(bottle) if not is_ipv4_literal(h)]


def pipelock_bottle_ssh_ip_cidrs(bottle: Bottle) -> list[str]:
    return [f"{h}/32" for h in pipelock_bottle_ssh_hostnames(bottle) if is_ipv4_literal(h)]


def pipelock_effective_allowlist(bottle: Bottle) -> list[str]:
    """Deduplicated union of: baked-in defaults, bottle.egress.allowlist,
    bottle.ssh[].Hostname. Sorted for stability."""
    seen: dict[str, None] = {}
    for h in DEFAULT_ALLOWLIST:
        seen.setdefault(h, None)
    for h in pipelock_bottle_allowlist(bottle):
        if h:
            seen.setdefault(h, None)
    for h in pipelock_bottle_ssh_hostnames(bottle):
        if h:
            seen.setdefault(h, None)
    return sorted(seen.keys())


def pipelock_allowlist_summary(bottle: Bottle) -> str:
    """One-line summary for the y/N preflight display:
    "<N> hosts allowed (host1, host2, host3, +M more)"."""
    hosts = pipelock_effective_allowlist(bottle)
    count = len(hosts)
    if count == 0:
        return "0 hosts allowed (none)"
    show = count
    more = 0
    if count > 5:
        show = 3
        more = count - show
    joined = ", ".join(hosts[:show])
    if more > 0:
        return f"{count} hosts allowed ({joined}, +{more} more)"
    return f"{count} hosts allowed ({joined})"



# --- Config build + YAML render --------------------------------------------


def pipelock_build_config(bottle: Bottle) -> dict[str, object]:
    """Build the structured pipelock config dict the sidecar will load.

    Deliberately carries no env values, no secrets, no per-agent
    customization beyond the resolved hostname list. The shape mirrors
    the YAML pipelock expects on disk; `pipelock_render_yaml` serializes
    it. Tests assert on this dict; production code renders it."""
    cfg: dict[str, object] = {
        "version": 1,
        "mode": "strict",
        "enforce": True,
        "api_allowlist": pipelock_effective_allowlist(bottle),
        "forward_proxy": {"enabled": True},
    }
    trusted = pipelock_bottle_ssh_trusted_domains(bottle)
    if trusted:
        cfg["trusted_domains"] = trusted
    ip_cidrs = pipelock_bottle_ssh_ip_cidrs(bottle)
    if ip_cidrs:
        cfg["ssrf"] = {"ip_allowlist": ip_cidrs}
    cfg["dlp"] = {"include_defaults": True, "scan_env": True}
    # Body-scan enforcement is a separate pipelock section (each DLP
    # "surface" — body, MCP, response — has its own action). Pipelock's
    # built-in default for request_body_scanning is "warn" (forward
    # with a log line); claude-bottle's default is "block" so a hit
    # actually stops the request from leaving the egress network.
    cfg["request_body_scanning"] = {"action": bottle.egress.dlp_action}
    return cfg


def pipelock_render_yaml(cfg: dict[str, object]) -> str:
    """Render a pipelock config dict (as produced by
    `pipelock_build_config`) as YAML. Hand-rolled so we don't take a
    YAML-parser dependency for a fixed, narrow shape."""
    def _bool(b: object) -> str:
        return "true" if b else "false"

    lines: list[str] = []
    lines.append(f"version: {cfg['version']}")
    lines.append(f"mode: {cfg['mode']}")
    lines.append(f"enforce: {_bool(cfg['enforce'])}")
    lines.append("")
    lines.append("api_allowlist:")
    for h in cast(list[str], cfg["api_allowlist"]):
        lines.append(f'  - "{h}"')
    lines.append("")
    lines.append("forward_proxy:")
    fp = cast(dict[str, object], cfg["forward_proxy"])
    lines.append(f"  enabled: {_bool(fp['enabled'])}")
    lines.append("")
    if "trusted_domains" in cfg:
        lines.append("trusted_domains:")
        for td in cast(list[str], cfg["trusted_domains"]):
            lines.append(f'  - "{td}"')
        lines.append("")
    if "ssrf" in cfg:
        lines.append("ssrf:")
        ssrf = cast(dict[str, object], cfg["ssrf"])
        lines.append("  ip_allowlist:")
        for cidr in cast(list[str], ssrf["ip_allowlist"]):
            lines.append(f'    - "{cidr}"')
        lines.append("")
    lines.append("dlp:")
    dlp = cast(dict[str, object], cfg["dlp"])
    lines.append(f"  include_defaults: {_bool(dlp['include_defaults'])}")
    lines.append(f"  scan_env: {_bool(dlp['scan_env'])}")
    lines.append("")
    lines.append("request_body_scanning:")
    rbs = cast(dict[str, object], cfg["request_body_scanning"])
    lines.append(f'  action: "{rbs["action"]}"')
    return "\n".join(lines) + "\n"


# --- Proxy class -----------------------------------------------------------


@dataclass(frozen=True)
class PipelockProxyPlan:
    """Output of PipelockProxy.prepare; consumed by .start when the
    sidecar needs to be brought up.

    yaml_path + slug are filled in at prepare time. internal_network
    and egress_network default to empty and are populated by the
    backend's launch step (via dataclasses.replace) once those networks
    have actually been created."""

    yaml_path: Path
    slug: str
    internal_network: str = ""
    egress_network: str = ""


class PipelockProxy(ABC):
    """The pipelock egress proxy. Encapsulates the YAML-config
    generation; the sidecar's start/stop lifecycle is backend-specific
    and lives on concrete subclasses."""

    def prepare(
        self, bottle: Bottle, slug: str, stage_dir: Path
    ) -> PipelockProxyPlan:
        """Write the pipelock yaml config (mode 600) under `stage_dir`
        and return the plan for `.start`.

        `slug` is the agent-derived identifier (lowercased,
        hyphen-normalized) used as the suffix in every per-agent
        resource name — the agent container, the pipelock container
        (`claude-bottle-pipelock-<slug>`), the internal/egress
        networks. It's stored on the returned plan so the backend's
        start step can derive the sidecar's container name."""
        yaml_path = stage_dir / "pipelock.yaml"
        self._build_pipelock_yaml(bottle, yaml_path)
        return PipelockProxyPlan(yaml_path=yaml_path, slug=slug)

    def _build_pipelock_yaml(self, bottle: Bottle, yaml_path: Path):
        """Write the pipelock yaml config (mode 600) to `yaml_path`."""
        yaml_path.write_text(pipelock_render_yaml(pipelock_build_config(bottle)))
        yaml_path.chmod(0o600)

    @abstractmethod
    def start(self, plan: PipelockProxyPlan) -> str:
        """Bring up the pipelock sidecar according to `plan`. Returns
        the proxy_target string identifying the running instance — the
        same value to pass to `.stop`. Backend-specific."""

    @abstractmethod
    def stop(self, proxy_target: str) -> None:
        """Tear down the pipelock sidecar identified by `proxy_target`
        (the value `.start` returned). Idempotent: a missing target is
        success. Backend-specific."""
