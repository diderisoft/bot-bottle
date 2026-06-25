"""Per-bottle egress proxy (PRD 0017, PRD 0053).

This module defines the abstract proxy (`Egress`), its plan
dataclass (`EgressPlan`), and the resolved per-route shape
(`EgressRoute`). The sidecar's start/stop lifecycle is backend-
specific and lives on concrete subclasses (see
`bot_bottle/backend/docker/egress.py`).
"""

from __future__ import annotations

import dataclasses
import secrets
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .egress_addon_core import (
    ON_MATCH_REDACT,
    HeaderMatch as CoreHeaderMatch,
    MatchEntry as CoreMatchEntry,
    PathMatch as CorePathMatch,
    Route,
)
from .log import die

if TYPE_CHECKING:
    from .manifest import ManifestBottle

CODEX_HOST_CREDENTIAL_TOKEN_REF = "BOT_BOTTLE_CODEX_HOST_ACCESS_TOKEN"

EGRESS_HOSTNAME = "egress"

EGRESS_ROUTES_IN_CONTAINER = "/etc/egress/routes.yaml"
EGRESS_ROUTES_FILENAME = Path(EGRESS_ROUTES_IN_CONTAINER).name

_CANARY_ENV_WORDS = (
    "ACCORD",
    "ANCHOR",
    "ATLAS",
    "CANON",
    "CIPHER",
    "EMBER",
    "FALCON",
    "HARBOR",
    "LANTERN",
    "MARBLE",
    "NOVA",
    "ORBIT",
    "PIVOT",
    "RADIUS",
    "SUMMIT",
    "VECTOR",
)


def _random_canary_env() -> str:
    first = secrets.choice(_CANARY_ENV_WORDS)
    remaining = tuple(word for word in _CANARY_ENV_WORDS if word != first)
    second = secrets.choice(remaining)
    return f"{first}_{second}_SECRET"


def egress_sidecar_env_entries(plan: "EgressPlan") -> tuple[str, ...]:
    """Return sidecar env entries needed by egress across all backends."""
    env: list[str] = []
    if plan.routes:
        env.extend(sorted(plan.token_env_map.keys()))
    if plan.canary and plan.canary_env:
        env.append(f"{plan.canary_env}={plan.canary}")
        env.append(f"BOT_BOTTLE_SENSITIVE_PREFIXES={plan.canary_env}")
    return tuple(env)


def egress_agent_env_entries(plan: "EgressPlan") -> tuple[str, ...]:
    """Return agent-visible egress env entries shared by all backends."""
    if plan.canary and plan.canary_env:
        return (f"{plan.canary_env}={plan.canary}",)
    return ()


@dataclass(frozen=True)
class EgressRoute(Route):
    """Host-side extension of the addon's `Route`.

    Inherits `host`, `matches`, `auth_scheme`, and `token_env`
    from `egress_addon_core.Route` — those are the fields that cross the
    YAML wire into the sidecar. The fields below are host-only and
    are never serialised to the addon.

    `token_ref` is the host env var the CLI reads at launch and forwards
    into the container's environ under `token_env`.

    `roles` carries the manifest route's role tuple (reserved for
    future use; always empty today)."""

    token_ref: str = ""
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class EgressPlan:
    slug: str
    routes_path: Path
    routes: tuple[EgressRoute, ...]
    token_env_map: dict[str, str]
    internal_network: str = ""
    egress_network: str = ""
    mitmproxy_ca_host_path: Path = Path()
    mitmproxy_ca_cert_only_host_path: Path = Path()
    log: int = 0
    canary: str = ""
    canary_env: str = ""


def egress_manifest_routes(
    bottle: ManifestBottle,
) -> tuple[EgressRoute, ...]:
    out: list[EgressRoute] = []
    for r in bottle.egress.routes:
        core_matches: list[CoreMatchEntry] = []
        for m in r.Matches:
            core_paths = tuple(
                CorePathMatch(type=p.Type, value=p.Value)
                for p in m.Paths
            )
            core_headers = tuple(
                CoreHeaderMatch(name=h.Name, value=h.Value, type=h.Type)
                for h in m.Headers
            )
            core_matches.append(CoreMatchEntry(
                paths=core_paths,
                methods=m.Methods,
                headers=core_headers,
            ))
        out.append(EgressRoute(
            host=r.Host,
            matches=tuple(core_matches),
            auth_scheme=r.AuthScheme,
            token_ref=r.TokenRef,
            roles=r.Role,
            git_fetch=r.GitFetch,
            outbound_detectors=r.OutboundDetectors,
            inbound_detectors=r.InboundDetectors,
            outbound_on_match=r.OutboundOnMatch,
        ))
    return tuple(out)


def egress_routes_for_bottle(
    bottle: ManifestBottle,
    provider_routes: tuple[EgressRoute, ...] = (),
) -> tuple[EgressRoute, ...]:
    manifest = egress_manifest_routes(bottle)
    provisioned_hosts = {pr.host.lower() for pr in provider_routes}
    merged = list(_default_provider_on_match(provider_routes)) + [
        r for r in manifest if r.host.lower() not in provisioned_hosts
    ]
    return _assign_token_slots(merged)


def _default_provider_on_match(
    provider_routes: tuple[EgressRoute, ...],
) -> tuple[EgressRoute, ...]:
    """Provider routes (the agent talking to its own LLM API) default to the
    `redact` on-match policy (PRD 0062): high-volume conversation payloads are
    the worst source of token-shaped false positives, so a match is scrubbed
    and forwarded rather than hard-blocked or queued for the operator. A
    provider that sets `outbound_on_match` explicitly keeps its choice."""
    return tuple(
        r if r.outbound_on_match
        else dataclasses.replace(r, outbound_on_match=ON_MATCH_REDACT)
        for r in provider_routes
    )


def _assign_token_slots(
    routes: list[EgressRoute],
) -> tuple[EgressRoute, ...]:
    slot_for_ref: dict[str, str] = {}
    out: list[EgressRoute] = []
    for r in routes:
        if r.auth_scheme and r.token_ref:
            slot = slot_for_ref.get(r.token_ref)
            if slot is None:
                slot = f"EGRESS_TOKEN_{len(slot_for_ref)}"
                slot_for_ref[r.token_ref] = slot
            out.append(dataclasses.replace(r, token_env=slot))
        else:
            out.append(r)
    return tuple(out)


def egress_token_env_map(
    routes: tuple[EgressRoute, ...],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in routes:
        if not (r.auth_scheme and r.token_ref and r.token_env):
            continue
        existing = out.get(r.token_env)
        if existing is not None and existing != r.token_ref:
            die(
                f"egress plan conflict: {r.token_env} maps to both "
                f"{existing!r} and {r.token_ref!r}. Two routes sharing a "
                f"token slot must reference the same host env var."
            )
        out[r.token_env] = r.token_ref
    return out


def _route_to_yaml_fields(r: Route) -> dict[str, object]:
    fields: dict[str, object] = {"host": r.host}
    if r.auth_scheme and r.token_env:
        fields["auth_scheme"] = r.auth_scheme
        fields["token_env"] = r.token_env
    if r.matches:
        matches_data: list[dict[str, object]] = []
        for entry in r.matches:
            entry_data: dict[str, object] = {}
            if entry.paths:
                paths_data: list[dict[str, str]] = []
                for pm in entry.paths:
                    pd: dict[str, str] = {"value": pm.value}
                    if pm.type != "prefix":
                        pd["type"] = pm.type
                    paths_data.append(pd)
                entry_data["paths"] = paths_data
            if entry.methods:
                entry_data["methods"] = list(entry.methods)
            if entry.headers:
                headers_data: list[dict[str, str]] = []
                for hm in entry.headers:
                    hd: dict[str, str] = {"name": hm.name, "value": hm.value}
                    if hm.type != "exact":
                        hd["type"] = hm.type
                    headers_data.append(hd)
                entry_data["headers"] = headers_data
            matches_data.append(entry_data)
        fields["matches"] = matches_data
    if r.git_fetch:
        fields["git"] = {"fetch": True}
    if (
        r.outbound_detectors is not None
        or r.inbound_detectors is not None
        or r.outbound_on_match
    ):
        dlp: dict[str, object] = {}
        if r.outbound_detectors is not None:
            dlp["outbound_detectors"] = (
                False if not r.outbound_detectors
                else list(r.outbound_detectors)
            )
        if r.inbound_detectors is not None:
            dlp["inbound_detectors"] = (
                False if not r.inbound_detectors
                else list(r.inbound_detectors)
            )
        if r.outbound_on_match:
            dlp["outbound_on_match"] = r.outbound_on_match
        fields["dlp"] = dlp
    return fields


def _render_match_entry(entry: dict[str, object]) -> list[str]:
    lines: list[str] = []
    first_key = True
    if "paths" in entry:
        lines.append("      - paths:")
        first_key = False
        for pd in entry["paths"]:  # type: ignore[union-attr]
            pd_dict: dict[str, str] = pd  # type: ignore[assignment]
            if "type" in pd_dict:
                lines.append(f'          - type: "{pd_dict["type"]}"')
                lines.append(f'            value: "{pd_dict["value"]}"')
            else:
                lines.append(f'          - value: "{pd_dict["value"]}"')
    if "methods" in entry:
        methods_str = ", ".join(f'"{m}"' for m in entry["methods"])  # type: ignore[union-attr]
        prefix = "      - " if first_key else "        "
        lines.append(f'{prefix}methods: [{methods_str}]')
        first_key = False
    if "headers" in entry:
        prefix = "      - " if first_key else "        "
        lines.append(f"{prefix}headers:")
        first_key = False
        for hd in entry["headers"]:  # type: ignore[union-attr]
            hd_dict: dict[str, str] = hd  # type: ignore[assignment]
            lines.append(f'          - name: "{hd_dict["name"]}"')
            lines.append(f'            value: "{hd_dict["value"]}"')
    if first_key:
        lines.append("      - {}")
    return lines


def egress_render_routes(
    routes: tuple[EgressRoute, ...],
    *,
    log: int = 0,
) -> str:
    lines: list[str] = []
    if log:
        lines.append(f"log: {log}")
    lines.append("routes:")
    if not routes:
        lines[-1] = "routes: []"
        return "\n".join(lines) + "\n"
    for r in routes:
        f = _route_to_yaml_fields(r)
        lines.append(f'  - host: "{f["host"]}"')
        if "auth_scheme" in f:
            lines.append(f'    auth_scheme: "{f["auth_scheme"]}"')
            lines.append(f'    token_env: "{f["token_env"]}"')
        if "matches" in f:
            lines.append("    matches:")
            for entry in f["matches"]:  # type: ignore[union-attr]
                lines.extend(_render_match_entry(entry))  # type: ignore[arg-type]
        if "git" in f:
            git_dict: dict[str, object] = f["git"]  # type: ignore
            lines.append("    git:")
            if git_dict.get("fetch") is True:
                lines.append("      fetch: true")
        if "dlp" in f:
            dlp_dict: dict[str, object] = f["dlp"]  # type: ignore
            lines.append("    dlp:")
            for dk, dv in dlp_dict.items():
                if dv is False:
                    lines.append(f"      {dk}: false")
                elif isinstance(dv, list):
                    items_str = ", ".join(f'"{x}"' for x in dv)
                    lines.append(f"      {dk}: [{items_str}]")
                elif isinstance(dv, str):
                    lines.append(f'      {dk}: "{dv}"')
    return "\n".join(lines) + "\n"


def egress_resolve_token_values(
    token_env_map: dict[str, str],
    host_env: dict[str, str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for token_env, token_ref in token_env_map.items():
        value = host_env.get(token_ref)
        if value is None:
            die(
                f"egress: host env var '{token_ref}' is unset. Set it "
                f"before launching, or remove the corresponding auth block "
                f"from bottle.egress.routes."
            )
        if not value:
            die(
                f"egress: host env var '{token_ref}' is empty. The "
                f"egress will not inject an empty token; set it to "
                f"the real value or remove the route's auth block."
            )
        out[token_env] = value
    return out


class Egress(ABC):
    def prepare(
        self,
        bottle: ManifestBottle,
        slug: str,
        stage_dir: Path,
        provider_routes: tuple[EgressRoute, ...] = (),
    ) -> EgressPlan:
        routes = egress_routes_for_bottle(bottle, provider_routes)
        log = bottle.egress.Log
        routes_path = stage_dir / EGRESS_ROUTES_FILENAME
        routes_path.write_text(egress_render_routes(routes, log=log))
        routes_path.chmod(0o600)
        # Generate a per-session fake secret under a plausible random env name.
        # The sidecar marks that exact env name as sensitive for known-secret
        # scanning; the agent receives the same name/value as exfil bait.
        canary = secrets.token_urlsafe(32)
        return EgressPlan(
            slug=slug,
            routes_path=routes_path,
            routes=routes,
            token_env_map=egress_token_env_map(routes),
            log=log,
            canary=canary,
            canary_env=_random_canary_env(),
        )

__all__ = [
    "CODEX_HOST_CREDENTIAL_TOKEN_REF",
    "EGRESS_HOSTNAME",
    "EGRESS_ROUTES_FILENAME",
    "EGRESS_ROUTES_IN_CONTAINER",
    "Egress",
    "EgressPlan",
    "EgressRoute",
    "egress_manifest_routes",
    "egress_render_routes",
    "egress_resolve_token_values",
    "egress_routes_for_bottle",
    "egress_agent_env_entries",
    "egress_sidecar_env_entries",
    "egress_token_env_map",
]
