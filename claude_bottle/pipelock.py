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

from .cred_proxy import CRED_PROXY_HOSTNAME
from .supervise import SUPERVISE_HOSTNAME
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
    the cred-proxy upstream hosts derived from bottle.cred_proxy.routes,
    the cred-proxy sidecar's own hostname when any cred_proxy route is
    declared, and the supervise sidecar's hostname when bottle.supervise
    is enabled. Sorted for stability. Git upstreams declared in
    `bottle.git` do NOT contribute here — git traffic flows through the
    per-agent git-gate sidecar (PRD 0008), not pipelock.

    The cred-proxy + supervise hostnames are auto-added because the
    agent's HTTP_PROXY points at pipelock, so a manifest-driven URL
    like `http://cred-proxy:9099/anthropic/...` or
    `http://supervise:9100/` arrives at pipelock as a request for the
    sidecar hostname. Without this auto-allow, pipelock would 403 the
    request before it reached the sidecar."""
    seen: dict[str, None] = {}
    for h in DEFAULT_ALLOWLIST:
        seen.setdefault(h, None)
    for h in pipelock_bottle_allowlist(bottle):
        if h:
            seen.setdefault(h, None)
    for h in pipelock_token_hosts(bottle):
        seen.setdefault(h, None)
    if bottle.cred_proxy.routes:
        seen.setdefault(CRED_PROXY_HOSTNAME, None)
    if bottle.supervise:
        seen.setdefault(SUPERVISE_HOSTNAME, None)
    return sorted(seen.keys())


def pipelock_seed_phrase_detection_enabled(bottle: Bottle) -> bool:
    """Whether pipelock's BIP-39 seed-phrase detector stays on for
    this bottle.

    LLM conversation bodies legitimately trip the detector — any 12+
    English words that pass the BIP-39 checksum match — so any
    bottle that routes claude through pipelock's body scanner gets
    blocked on the first real chat. We tried two narrower knobs
    first:

      - `suppress: [{rule, path}]` — pipelock accepts the schema
        but the entry only silences the alert; the body_dlp block
        still fires.
      - `rules.disabled: ["dlp:BIP-39 Seed Phrase"]` — same shape,
        same outcome: 403 still returned.

    Empirically only `seed_phrase_detection.enabled: false`
    actually stops the block (verified by sending a 12-word BIP-39
    body through three pipelock instances). It is a global toggle
    — there is no per-path / per-host knob in pipelock 2.3.0 — so
    we turn the detector off for the entire bottle when an
    `anthropic-base-url` route is declared. The trade-off is
    accepted: BIP-39 detection has little value in claude-bottle's
    threat model (the agent has no access to a user's crypto
    wallet seeds; the patterns that matter — gh*_, sk-ant-, AKIA,
    etc. — keep firing)."""
    return not any(
        "anthropic-base-url" in r.Role for r in bottle.cred_proxy.routes
    )


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
    ssrf_ip_allowlist: tuple[str, ...] = (),
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
    by PRD 0006 to turn on pipelock's native TLS interception.

    `ssrf_ip_allowlist` is the list of IPs / CIDRs that bypass
    pipelock's SSRF guard. Pipelock blocks RFC1918-resolved
    destinations by default, which would catch the agent's
    cred-proxy traffic (cred-proxy sits on the bottle's internal
    Docker network in 172.x space). Pass the bottle's internal
    network CIDR here so `cred-proxy:9099` requests get through
    pipelock while api_allowlist + body-scanning still apply. Empty
    by default; omitted from the rendered yaml when empty so
    pipelock keeps its built-in SSRF defaults."""
    cfg: dict[str, object] = {
        "version": 1,
        "mode": "strict",
        "enforce": True,
        "api_allowlist": pipelock_effective_allowlist(bottle),
        "forward_proxy": {"enabled": True},
    }
    if not pipelock_seed_phrase_detection_enabled(bottle):
        cfg["seed_phrase_detection"] = {"enabled": False}
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
    if ssrf_ip_allowlist:
        cfg["ssrf"] = {"ip_allowlist": list(ssrf_ip_allowlist)}
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
    if "seed_phrase_detection" in cfg:
        lines.append("seed_phrase_detection:")
        spd = cast(dict[str, object], cfg["seed_phrase_detection"])
        lines.append(f"  enabled: {_bool(spd['enabled'])}")
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
    if "ssrf" in cfg:
        lines.append("")
        lines.append("ssrf:")
        ssrf = cast(dict[str, object], cfg["ssrf"])
        lines.append("  ip_allowlist:")
        for ip in cast(list[str], ssrf["ip_allowlist"]):
            lines.append(f'    - "{ip}"')
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
    those networks exist, the CA host paths once the one-shot
    `pipelock tls init` has run, and `internal_network_cidr` once
    Docker has assigned a subnet to the internal network. Empty
    defaults are sentinels meaning "not yet set"; `.start` validates
    that they are populated.

    `internal_network_cidr` ends up on pipelock's `ssrf.ip_allowlist`
    so the agent's requests at `cred-proxy:9099` (or any other
    bottle-internal sidecar) bypass pipelock's RFC1918 SSRF guard
    while api_allowlist and body-scanning still apply."""

    yaml_path: Path
    slug: str
    internal_network: str = ""
    internal_network_cidr: str = ""
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
