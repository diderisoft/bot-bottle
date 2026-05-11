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
    and lives on concrete subclasses (e.g. DockerPipelockProxy)."""

    def prepare(
        self, bottle: Bottle, slug: str, yaml_path: Path
    ) -> PipelockProxyPlan:
        """Write the pipelock yaml config (mode 600) to `yaml_path`
        and return the plan for `.start`.

        `slug` is the agent-derived identifier (lowercased,
        hyphen-normalized) used as the suffix in every per-agent
        resource name — the agent container, the pipelock container
        (`claude-bottle-pipelock-<slug>`), the internal/egress
        networks. It's stored on the returned plan so the backend's
        start step can derive the sidecar's container name."""
        self._build_pipelock_yaml(bottle, yaml_path)
        return PipelockProxyPlan(yaml_path=yaml_path, slug=slug)

    def _build_pipelock_yaml(self, bottle: Bottle, yaml_path: Path):
        """Write the pipelock yaml config (mode 600) to `yaml_path`
        for the sidecar to consume when it boots. Carries the
        effective allowlist (bottle.egress.allowlist UNION
        claude-bottle defaults UNION ssh hostnames), a fixed listen
        port, strict mode + forward_proxy + DLP defaults + scan_env.
        Deliberately contains no env values, no secrets, no per-agent
        customization beyond the hostname list."""
        allowlist = pipelock_effective_allowlist(bottle)
        trusted = pipelock_bottle_ssh_trusted_domains(bottle)
        ip_cidrs = pipelock_bottle_ssh_ip_cidrs(bottle)

        lines: list[str] = []
        lines.append("version: 1")
        lines.append("mode: strict")
        lines.append("enforce: true")
        lines.append("")
        lines.append("# Hostnames the agent is allowed to reach. Effective list is")
        lines.append("# claude-bottle defaults UNION bottle.egress.allowlist (sorted, deduped).")
        lines.append("api_allowlist:")
        for h in allowlist:
            lines.append(f'  - "{h}"')
        lines.append("")
        lines.append("forward_proxy:")
        lines.append("  enabled: true")
        lines.append("")
        if trusted:
            lines.append("trusted_domains:")
            for td in trusted:
                lines.append(f'  - "{td}"')
            lines.append("")
        if ip_cidrs:
            lines.append("ssrf:")
            lines.append("  ip_allowlist:")
            for cidr in ip_cidrs:
                lines.append(f'    - "{cidr}"')
            lines.append("")
        lines.append("dlp:")
        lines.append("  include_defaults: true")
        lines.append("  scan_env: true")

        yaml_path.write_text("\n".join(lines) + "\n")
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
