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

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .log import die, info, warn
from .manifest import Bottle, Manifest
from .util import is_ipv4_literal

# Pipelock image, pinned by digest. The digest is the multi-arch image
# index for ghcr.io/luckypipewrench/pipelock:2.3.0.
PIPELOCK_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_PIPELOCK_IMAGE",
    "ghcr.io/luckypipewrench/pipelock@sha256:3b1a39417b98406ddc5dc2d8fcb42865ddc0c68a43d355db55f0f8cb06bc6de9",
)

# Listening port for pipelock's forward proxy.
PIPELOCK_PORT = os.environ.get("CLAUDE_BOTTLE_PIPELOCK_PORT", "8888")

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


def pipelock_container_name(slug: str) -> str:
    return f"claude-bottle-pipelock-{slug}"


def pipelock_proxy_url(slug: str) -> str:
    return f"http://{pipelock_container_name(slug)}:{PIPELOCK_PORT}"


def pipelock_proxy_host_port(slug: str) -> str:
    return f"{pipelock_container_name(slug)}:{PIPELOCK_PORT}"


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


class PipelockProxy:
    """The pipelock egress proxy. Encapsulates the YAML-config
    generation and the sidecar's start/stop lifecycle."""

    def prepare(
        self, manifest: Manifest, bottle_name: str, slug: str, yaml_path: Path
    ) -> PipelockProxyPlan:
        """Write the pipelock yaml config (mode 600) to `yaml_path`
        for the sidecar to consume when it boots. Carries the
        effective allowlist (bottle.egress.allowlist UNION
        claude-bottle defaults UNION ssh hostnames), a fixed listen
        port, strict mode + forward_proxy + DLP defaults + scan_env.
        Deliberately contains no env values, no secrets, no per-agent
        customization beyond the hostname list."""
        return self._build_pipelock_yaml(manifest, bottle_name, slug, yaml_path)

    def _build_pipelock_yaml(
        self, manifest: Manifest, bottle_name: str, slug: str, yaml_path: Path
    ) -> PipelockProxyPlan:
        bottle = manifest.bottles[bottle_name]
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

        return PipelockProxyPlan(yaml_path=yaml_path, slug=slug)

    def start(self, plan: PipelockProxyPlan) -> str:
        """Boot the pipelock sidecar:
          1. `docker create` on the internal network with the canonical
             name and argv `run --config /etc/pipelock.yaml --listen
             0.0.0.0:<port>`.
          2. `docker cp` the YAML config to /etc/pipelock.yaml in the
             writable layer (parent dir must already exist; image is
             distroless).
          3. Attach to the per-agent egress network.
          4. `docker start`.
        Returns the container name (the proxy_target passed to .stop)."""
        name = pipelock_container_name(plan.slug)
        if not plan.yaml_path.is_file():
            die(
                f"pipelock yaml not found at {plan.yaml_path}; "
                f"PipelockProxy.prepare must run first"
            )

        info(f"starting pipelock sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            PIPELOCK_IMAGE,
            "run", "--config", "/etc/pipelock.yaml",
            "--listen", f"0.0.0.0:{PIPELOCK_PORT}",
        ]
        if subprocess.run(create_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            die(f"failed to create pipelock sidecar {name}")

        cp_result = subprocess.run(
            ["docker", "cp", str(plan.yaml_path), f"{name}:/etc/pipelock.yaml"],
            capture_output=True,
            text=True,
        )
        if cp_result.returncode != 0:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            die(f"failed to copy pipelock yaml into {name}: {cp_result.stderr.strip()}")

        if subprocess.run(
            ["docker", "network", "connect", plan.egress_network, name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode != 0:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            die(f"failed to attach pipelock sidecar {name} to egress network {plan.egress_network}")

        if subprocess.run(
            ["docker", "start", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode != 0:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            die(f"failed to start pipelock sidecar {name}")

        return name

    def stop(self, proxy_target: str) -> None:
        """Idempotent: missing container is success. `proxy_target` is
        the container name returned by .start."""
        if subprocess.run(
            ["docker", "inspect", proxy_target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            if subprocess.run(
                ["docker", "rm", "-f", proxy_target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode != 0:
                warn(
                    f"failed to remove pipelock sidecar {proxy_target}; "
                    f"clean up with 'docker rm -f {proxy_target}'"
                )
