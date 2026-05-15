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

# Hosts pipelock should NOT TLS-MITM, even when tls_interception is
# enabled. The Claude API endpoint is an LLM provider — its request
# bodies are user-authored conversation text that legitimately can
# trigger DLP scanners (notably the BIP-39 seed-phrase detector, which
# fires on any 12+ consecutive English words that happen to be on the
# BIP-39 wordlist and pass the checksum). Per pipelock's own
# configuration.md, the recommended treatment for LLM API endpoints is
# `passthrough_domains`: pipelock still proxies the CONNECT (so the
# api_allowlist gate applies), but it does not generate a leaf cert or
# decrypt the body. Body scanning happens on hosts that aren't
# passthrough'd, so DLP protection against agent exfil to other
# allowlisted hosts is unchanged.
DEFAULT_TLS_PASSTHROUGH: tuple[str, ...] = (
    "api.anthropic.com",
)


# --- Allowlist resolution --------------------------------------------------


def pipelock_bottle_allowlist(bottle: Bottle) -> list[str]:
    """Hostnames in bottle.egress.allowlist."""
    return list(bottle.egress.allowlist)


def pipelock_token_hosts(bottle: Bottle) -> list[str]:
    """Hostnames the cred-proxy sidecar (PRD 0010) talks to upstream
    on the agent's behalf. Derived from each route's
    `upstream.UpstreamHost` in `bottle.cred_proxy.routes`. Returned
    sorted+deduped.

    These hosts must be on pipelock's allowlist so cred-proxy's
    outbound HTTPS traffic can leave the egress network. They are
    NOT auto-added to passthrough_domains: cred-proxy's HTTPS client
    trusts pipelock's per-bottle CA at runtime (installed via
    docker cp + update-ca-certificates in the cred-proxy image),
    so pipelock MITMs and body-scans the cred-proxy → upstream leg
    the same way it does direct agent traffic."""
    hosts = {r.UpstreamHost for r in bottle.cred_proxy.routes if r.UpstreamHost}
    return sorted(hosts)


def pipelock_effective_allowlist(bottle: Bottle) -> list[str]:
    """Deduplicated union of: baked-in defaults, bottle.egress.allowlist,
    and the cred-proxy upstream hosts derived from bottle.cred_proxy.routes.
    Sorted for stability. Git upstreams declared in `bottle.git` do NOT
    contribute here — git traffic flows through the per-agent git-gate
    sidecar (PRD 0008), not pipelock."""
    seen: dict[str, None] = {}
    for h in DEFAULT_ALLOWLIST:
        seen.setdefault(h, None)
    for h in pipelock_bottle_allowlist(bottle):
        if h:
            seen.setdefault(h, None)
    for h in pipelock_token_hosts(bottle):
        seen.setdefault(h, None)
    return sorted(seen.keys())


def pipelock_effective_tls_passthrough(bottle: Bottle) -> list[str]:
    """Hostnames pipelock should pass through (no TLS MITM, no body
    scan). Default carries the LLM API endpoint — its request bodies
    are user-authored conversation text that legitimately trips DLP
    scanners (notably pipelock's BIP-39 seed-phrase detector). Every
    other allowlisted host is MITM'd by pipelock's per-bottle CA so
    its body scanner sees the cleartext.

    cred-proxy upstream hosts (github, gitea, npm) are deliberately
    NOT auto-added here. cred-proxy's HTTPS client trusts pipelock's
    CA at runtime (folded into its trust store via docker cp +
    update-ca-certificates), so pipelock can MITM the cred-proxy →
    upstream leg and body-scan it the same way it body-scans the
    agent's direct HTTPS traffic. Without this, an agent that pushed
    a secret via cred-proxy's /gh-git/ path would have no body
    scanner in front of it. The PRD's earlier reasoning that
    cred-proxy hosts needed passthrough was a workaround for the
    cert-trust gap that no longer exists.

    `bottle` is kept on the signature for forward-compat (a future
    knob might let a manifest opt a host into passthrough); today
    the returned list is independent of the bottle."""
    del bottle  # not consulted; see docstring.
    return sorted(DEFAULT_TLS_PASSTHROUGH)


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


def pipelock_build_config(
    bottle: Bottle,
    *,
    ca_cert_path: str = "",
    ca_key_path: str = "",
) -> dict[str, object]:
    """Build the structured pipelock config dict the sidecar will load.

    Deliberately carries no env values, no secrets, no per-agent
    customization beyond the resolved hostname list. The shape mirrors
    the YAML pipelock expects on disk; `pipelock_render_yaml` serializes
    it. Tests assert on this dict; production code renders it.

    `ca_cert_path` / `ca_key_path` are the **in-container** paths the
    pipelock sidecar will read its CA from at runtime (they're
    populated into the container at start time via `docker cp`).
    Pass both or neither: both → emit `tls_interception` block with
    `enabled: true`; neither → omit the block entirely (pipelock
    falls back to its built-in default of `enabled: false`). Used
    by PRD 0006 to turn on pipelock's native TLS interception."""
    cfg: dict[str, object] = {
        "version": 1,
        "mode": "strict",
        "enforce": True,
        "api_allowlist": pipelock_effective_allowlist(bottle),
        "forward_proxy": {"enabled": True},
    }
    cfg["dlp"] = {"include_defaults": True, "scan_env": True}
    # Body-scan enforcement is a separate pipelock section (each DLP
    # "surface" — body, MCP, response — has its own action). Pipelock's
    # built-in default for request_body_scanning is "warn" (forward
    # with a log line); claude-bottle's default is "block" so a hit
    # actually stops the request from leaving the egress network.
    cfg["request_body_scanning"] = {"action": bottle.egress.dlp_action}
    if ca_cert_path or ca_key_path:
        if not (ca_cert_path and ca_key_path):
            raise ValueError(
                "pipelock_build_config: pass both ca_cert_path and ca_key_path "
                "to enable tls_interception, or neither to leave it off"
            )
        cfg["tls_interception"] = {
            "enabled": True,
            "ca_cert": ca_cert_path,
            "ca_key": ca_key_path,
            "passthrough_domains": pipelock_effective_tls_passthrough(bottle),
        }
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
    lines.append("dlp:")
    dlp = cast(dict[str, object], cfg["dlp"])
    lines.append(f"  include_defaults: {_bool(dlp['include_defaults'])}")
    lines.append(f"  scan_env: {_bool(dlp['scan_env'])}")
    lines.append("")
    lines.append("request_body_scanning:")
    rbs = cast(dict[str, object], cfg["request_body_scanning"])
    lines.append(f'  action: "{rbs["action"]}"')
    if "tls_interception" in cfg:
        lines.append("")
        lines.append("tls_interception:")
        tls = cast(dict[str, object], cfg["tls_interception"])
        lines.append(f"  enabled: {_bool(tls['enabled'])}")
        lines.append(f'  ca_cert: "{tls["ca_cert"]}"')
        lines.append(f'  ca_key: "{tls["ca_key"]}"')
        passthrough = cast(list[str], tls.get("passthrough_domains", []))
        if passthrough:
            lines.append("  passthrough_domains:")
            for d in passthrough:
                lines.append(f'    - "{d}"')
    return "\n".join(lines) + "\n"


# --- Proxy class -----------------------------------------------------------


@dataclass(frozen=True)
class PipelockProxyPlan:
    """Output of PipelockProxy.prepare; consumed by .start when the
    sidecar needs to be brought up.

    yaml_path + slug are filled in at prepare time (host-side, side-
    effect-free; the YAML references the in-container CA paths
    already so it doesn't need the host paths to be valid). The
    remaining fields are populated by the backend's launch step
    via `dataclasses.replace`: internal/egress networks once
    those networks exist, and the CA host paths once the
    one-shot `pipelock tls init` has run. Empty defaults are
    sentinels meaning "not yet set"; `.start` validates that
    they are populated."""

    yaml_path: Path
    slug: str
    internal_network: str = ""
    egress_network: str = ""
    ca_cert_host_path: Path = Path()
    ca_key_host_path: Path = Path()


class PipelockProxy(ABC):
    """The pipelock egress proxy. Encapsulates the YAML-config
    generation; the sidecar's start/stop lifecycle is backend-specific
    and lives on concrete subclasses.

    The class-level constants `CA_CERT_IN_CONTAINER` /
    `CA_KEY_IN_CONTAINER` are the in-container paths the YAML config
    references — they correspond to wherever the backend's `.start`
    places the CA cert and key inside the sidecar. Subclasses
    override the constants."""

    CA_CERT_IN_CONTAINER: str = ""
    CA_KEY_IN_CONTAINER: str = ""

    def prepare(
        self, bottle: Bottle, slug: str, stage_dir: Path
    ) -> PipelockProxyPlan:
        """Write the pipelock yaml config (mode 600) under `stage_dir`
        and return the plan for `.start`. Pure host-side, no docker
        subprocess.

        `slug` is the agent-derived identifier (lowercased,
        hyphen-normalized) used as the suffix in every per-agent
        resource name — the agent container, the pipelock container
        (`claude-bottle-pipelock-<slug>`), the internal/egress
        networks. It's stored on the returned plan so the backend's
        start step can derive the sidecar's container name.

        The CA paths the YAML references are the in-container paths
        from the concrete subclass's class-level constants. The
        host-side counterparts are generated by the launch step
        (not here, so prepare stays side-effect-free on docker) and
        added to the plan via `dataclasses.replace` before `.start`."""
        yaml_path = stage_dir / "pipelock.yaml"
        cfg = pipelock_build_config(
            bottle,
            ca_cert_path=self.CA_CERT_IN_CONTAINER,
            ca_key_path=self.CA_KEY_IN_CONTAINER,
        )
        yaml_path.write_text(pipelock_render_yaml(cfg))
        yaml_path.chmod(0o600)
        return PipelockProxyPlan(yaml_path=yaml_path, slug=slug)

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
