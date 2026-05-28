"""Pipelock sidecar lifecycle for the per-agent egress topology.

Pipelock (https://github.com/luckyPipewrench/pipelock) is an HTTP
forward proxy with hostname allowlisting + DLP scanning + URL-entropy
checks. One sidecar per agent, attached to the agent's --internal
network and a per-agent user-defined egress bridge.

Post-PRD-0017 topology: the agent's HTTP_PROXY points at egress
(not pipelock); egress sets `HTTPS_PROXY=pipelock` on its
outbound leg. So pipelock no longer sees the agent's connections
directly — it sees the egress → upstream leg, applies the
hostname allowlist + DLP body scan there, and forwards to the real
upstream.

Image pin: ghcr.io/luckypipewrench/pipelock@sha256:<digest> for tag 2.3.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .egress import EGRESS_HOSTNAME, egress_routes_for_bottle
from .supervise import SUPERVISE_HOSTNAME
from .manifest import Bottle

# Hosts pipelock should NOT TLS-MITM, even when tls_interception is
# enabled. This is now route-owned manifest policy via
# `egress.routes[].pipelock.tls_passthrough`; no provider hosts are
# injected implicitly.
DEFAULT_TLS_PASSTHROUGH: tuple[str, ...] = ()


# In-container paths the rendered pipelock YAML references under
# `tls_interception`. The pipelock binary expects the per-bottle CA
# cert + key at these exact paths inside its container — independent
# of how the daemon is wrapped (own container, sidecar bundle, etc.),
# which is why they live in the platform-neutral module.
PIPELOCK_CA_CERT_IN_CONTAINER = "/etc/pipelock-ca.pem"
PIPELOCK_CA_KEY_IN_CONTAINER = "/etc/pipelock-ca-key.pem"


# Short network alias for pipelock inside the sidecar bundle. The
# agent's HTTP_PROXY (when no egress is declared) and any in-bundle
# consumer's URL both reference this name.
PIPELOCK_HOSTNAME = "pipelock"


# --- Allowlist resolution --------------------------------------------------


def pipelock_effective_allowlist(bottle: Bottle) -> list[str]:
    """Hostnames pipelock allows. Sorted for stability.

    Always mirrors `egress_routes_for_bottle(bottle)` — egress is the
    single allowlist surface, and pipelock's allowlist is the downstream
    copy for defense-in-depth + DLP body scanning. For bottles without
    any `egress.routes[]` declared, this is empty except for supervise
    sidecar traffic when `supervise: true`.

    The supervise sidecar's hostname is auto-added when supervise
    is enabled (sibling-sidecar traffic that flows through pipelock
    would otherwise be 403'd). Git upstreams declared in
    `bottle.git` do NOT contribute here — git traffic flows
    through git-gate (PRD 0008), not pipelock."""
    seen: dict[str, None] = {}
    for r in egress_routes_for_bottle(bottle):
        if r.host:
            seen.setdefault(r.host, None)
    if bottle.supervise:
        seen.setdefault(SUPERVISE_HOSTNAME, None)
    return sorted(seen.keys())


def pipelock_seed_phrase_detection_enabled(bottle: Bottle) -> bool:
    """Whether pipelock's BIP-39 seed-phrase detector stays on.

    LLM conversation bodies legitimately trip the detector — any 12+
    English words that pass the BIP-39 checksum match — so agents can
    get blocked on ordinary prompts/responses regardless of provider
    (Claude, Codex/OpenAI, or future harnesses). We tried two narrower
    knobs first:

      - `suppress: [{rule, path}]` — pipelock accepts the schema
        but the entry only silences the alert; the body_dlp block
        still fires.
      - `rules.disabled: ["dlp:BIP-39 Seed Phrase"]` — same shape,
        same outcome: 403 still returned.

    Empirically only `seed_phrase_detection.enabled: false`
    actually stops the block (verified by sending a 12-word BIP-39
    body through three pipelock instances). It is a global toggle —
    no per-path / per-host knob in pipelock 2.3.0 — so we turn off
    only this detector for every bottle. The rest of pipelock's DLP
    defaults and request-body/header scanning remain enabled."""
    del bottle  # kept for call-site stability and future policy knobs.
    return False


def pipelock_effective_tls_passthrough(bottle: Bottle) -> list[str]:
    """Hostnames pipelock should pass through (no TLS MITM).

    A route opts in with `pipelock.tls_passthrough: true`. This is
    useful for provider API routes where egress injects the
    Authorization header after the agent boundary; pipelock still
    enforces the host allowlist but does not decrypt and scan that
    provider request.
    """
    seen: dict[str, None] = {host: None for host in DEFAULT_TLS_PASSTHROUGH}
    for route in bottle.egress.routes:
        if route.Pipelock.TlsPassthrough:
            seen.setdefault(route.Host, None)
    return sorted(seen.keys())





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
    destinations by default, which would catch sibling-sidecar
    traffic on the bottle's internal Docker network in 172.x space
    (e.g. egress → pipelock on the upstream leg). Pass the
    bottle's internal network CIDR here so internal-network requests
    pass through pipelock while api_allowlist + body-scanning still
    apply. Empty by default; omitted from the rendered yaml when
    empty so pipelock keeps its built-in SSRF defaults."""
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
    # with a log line); bot-bottle hard-codes "block" so a hit
    # actually stops the request from leaving the egress network.
    #
    # `scan_headers: true` + `header_mode: all` extends the scan to
    # every request header — pipelock's default `header_mode:
    # sensitive` only checks Authorization / Cookie / X-Api-Key /
    # X-Token / Proxy-Authorization / X-Goog-Api-Key, which an
    # agent attempting to exfil could trivially avoid by picking
    # a non-sensitive header name. "all" closes the gap; pipelock
    # caps it at the same max_body_bytes the body scan uses.
    cfg["request_body_scanning"] = {
        "action": "block",
        "scan_headers": True,
        "header_mode": "all",
    }
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
    if "scan_headers" in rbs:
        lines.append(f"  scan_headers: {_bool(rbs['scan_headers'])}")
    if "header_mode" in rbs:
        lines.append(f'  header_mode: "{rbs["header_mode"]}"')
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
    so traffic from sibling sidecars (egress → pipelock on the
    upstream leg, etc.) bypasses pipelock's RFC1918 SSRF guard while
    api_allowlist and body-scanning still apply."""

    yaml_path: Path
    slug: str
    internal_network: str = ""
    internal_network_cidr: str = ""
    egress_network: str = ""
    ca_cert_host_path: Path = Path()
    ca_key_host_path: Path = Path()


class PipelockProxy:
    """The pipelock egress proxy. Encapsulates the YAML-config
    generation; the container lifecycle is owned by whatever
    wraps the daemon (compose-managed pipelock container on docker,
    sidecar-bundle PID 1 on smolmachines).

    Backends instantiate the class directly — there are no
    platform-specific subclasses; the in-container CA paths are
    universal module-level constants
    (`PIPELOCK_CA_CERT_IN_CONTAINER` / `PIPELOCK_CA_KEY_IN_CONTAINER`)."""

    def prepare(
        self, bottle: Bottle, slug: str, stage_dir: Path
    ) -> PipelockProxyPlan:
        """Write the pipelock yaml config (mode 600) under `stage_dir`
        and return the plan for launch. Pure host-side, no docker
        subprocess.

        `slug` is the agent-derived identifier (lowercased,
        hyphen-normalized) used as the suffix in every per-agent
        resource name — the agent container, the sidecar bundle
        container, the internal/egress networks. It's stored on the
        returned plan so the backend's launch step can derive those
        names.

        The CA paths the YAML references are the module-level
        in-container constants. The host-side counterparts are
        generated by the launch step (not here, so prepare stays
        side-effect-free on docker) and added to the plan via
        `dataclasses.replace` before the daemon starts."""
        yaml_path = stage_dir / "pipelock.yaml"
        cfg = pipelock_build_config(
            bottle,
            ca_cert_path=PIPELOCK_CA_CERT_IN_CONTAINER,
            ca_key_path=PIPELOCK_CA_KEY_IN_CONTAINER,
        )
        yaml_path.write_text(pipelock_render_yaml(cfg))
        yaml_path.chmod(0o600)
        return PipelockProxyPlan(yaml_path=yaml_path, slug=slug)
