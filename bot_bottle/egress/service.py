"""The `Egress` host service: build a bottle's egress plan (routes, the
gateway's `routes.yaml`, token slots, the canary), resolve token values at
launch, and the env entries backends inject.
"""

from __future__ import annotations

import dataclasses
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from ..gateway.egress.dlp_config import ON_MATCH_REDACT
from ..gateway.egress.types import (
    HeaderMatch as CoreHeaderMatch,
    MatchEntry as CoreMatchEntry,
    LOG_FULL,
    PathMatch as CorePathMatch,
    Route,
)
from ..gateway.egress import full_log
from ..errors import MissingEnvVarError
from ..lib.log import die, warn
from ..secret_source import SecretSource
from .plan import EgressPlan, EgressRoute

if TYPE_CHECKING:
    from ..manifest import ManifestBottle


CODEX_HOST_CREDENTIAL_TOKEN_REF = "BOT_BOTTLE_CODEX_HOST_ACCESS_TOKEN"
CLAUDE_HOST_CREDENTIAL_TOKEN_REF = "BOT_BOTTLE_CLAUDE_HOST_ACCESS_TOKEN"

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


def egress_gateway_env_entries(plan: "EgressPlan") -> tuple[str, ...]:
    """Gateway env entries egress needs on every backend."""
    env: list[str] = []
    if plan.routes:
        env.extend(sorted(plan.token_env_map.keys()))
    if plan.canary and plan.canary_env:
        env.append(f"{plan.canary_env}={plan.canary}")
        env.append(f"BOT_BOTTLE_SENSITIVE_PREFIXES={plan.canary_env}")
    return tuple(env)


def egress_agent_env_entries(plan: "EgressPlan") -> tuple[str, ...]:
    """Agent-visible egress env entries shared by every backend."""
    if plan.canary and plan.canary_env:
        return (f"{plan.canary_env}={plan.canary}",)
    return ()



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
            auth_header=r.AuthHeader,
            token_ref=r.TokenRef,
            roles=r.Role,
            git_fetch=r.GitFetch,
            outbound_detectors=r.OutboundDetectors,
            inbound_detectors=r.InboundDetectors,
            outbound_on_match=r.OutboundOnMatch,
            preserve_auth=r.PreserveAuth,
            inspect=r.Inspect,
        ))
    return tuple(out)


def egress_routes_for_bottle(
    bottle: ManifestBottle,
    provider_routes: tuple[EgressRoute, ...] = (),
    owner: str = "",
) -> tuple[EgressRoute, ...]:
    """The provider's routes, then the bottle's. A bottle route for a host the
    provider owns fails the launch, naming `owner` and the host: the bottle
    was written without knowing the harness, so its route means something
    (detectors, `inspect`, method limits) that dropping it would hide."""
    manifest = egress_manifest_routes(bottle)
    provisioned_hosts = {pr.host.lower() for pr in provider_routes}
    clashes = sorted({r.host for r in manifest if r.host.lower() in provisioned_hosts})
    if clashes:
        where = f"{owner}: " if owner else ""
        die(f"{where}the bottle declares a route for {', '.join(clashes)}, which the "
            "agent's provider already routes; remove it from the bottle")
    return _assign_token_slots(list(_default_provider_on_match(provider_routes)) + list(manifest))


def _default_provider_on_match(
    provider_routes: tuple[EgressRoute, ...],
) -> tuple[EgressRoute, ...]:
    """Provider routes default to `redact`: conversation payloads produce the
    most token-shaped false positives. An explicit `outbound_on_match` wins."""
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
        if (r.auth_scheme or r.auth_header) and r.token_ref:
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
        if not (r.injects_auth and r.token_ref):
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


def _yaml_str_escape(s: str) -> str:
    """Escape a string for a YAML double-quoted scalar."""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
    )


def _route_to_yaml_fields(r: Route) -> dict[str, object]:
    fields: dict[str, object] = {"host": r.host}
    if not r.inspect:
        fields["inspect"] = False
        return fields
    inspect: dict[str, object] = {}
    if r.injects_auth:
        if r.auth_scheme:
            inspect["auth_scheme"] = r.auth_scheme
        if r.auth_header:
            inspect["auth_header"] = r.auth_header
        inspect["token_env"] = r.token_env
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
        inspect["matches"] = matches_data
    if r.git_fetch:
        inspect["git"] = {"fetch": True}
    if r.preserve_auth:
        inspect["preserve_auth"] = True
    if (
        r.outbound_detectors is not None
        or r.inbound_detectors is not None
        or r.outbound_on_match
    ):
        if r.outbound_detectors is not None:
            inspect["outbound_detectors"] = (
                False if not r.outbound_detectors
                else list(r.outbound_detectors)
            )
        if r.inbound_detectors is not None:
            inspect["inbound_detectors"] = (
                False if not r.inbound_detectors
                else list(r.inbound_detectors)
            )
        if r.outbound_on_match:
            inspect["outbound_on_match"] = r.outbound_on_match
    if inspect:
        fields["inspect"] = inspect
    return fields


def _render_match_entry(entry: dict[str, object]) -> list[str]:
    lines: list[str] = []
    first_key = True
    if "paths" in entry:
        lines.append("        - paths:")
        first_key = False
        for pd in entry["paths"]:  # type: ignore[union-attr]
            pd_dict: dict[str, str] = pd  # type: ignore[assignment]
            if "type" in pd_dict:
                lines.append(f'            - type: "{_yaml_str_escape(pd_dict["type"])}"')
                lines.append(f'              value: "{_yaml_str_escape(pd_dict["value"])}"')
            else:
                lines.append(f'            - value: "{_yaml_str_escape(pd_dict["value"])}"')
    if "methods" in entry:
        methods_str = ", ".join(f'"{_yaml_str_escape(m)}"' for m in entry["methods"])  # type: ignore[union-attr]
        prefix = "        - " if first_key else "          "
        lines.append(f'{prefix}methods: [{methods_str}]')
        first_key = False
    if "headers" in entry:
        prefix = "        - " if first_key else "          "
        lines.append(f"{prefix}headers:")
        first_key = False
        for hd in entry["headers"]:  # type: ignore[union-attr]
            hd_dict: dict[str, str] = hd  # type: ignore[assignment]
            lines.append(f'            - name: "{_yaml_str_escape(hd_dict["name"])}"')
            lines.append(f'              value: "{_yaml_str_escape(hd_dict["value"])}"')
    if first_key:
        lines.append("        - {}")
    return lines


def egress_render_routes(
    routes: tuple[EgressRoute, ...],
    *,
    log: int = 0,
    audit_delivery: str = "best_effort",
    firehose: str = "full",
) -> str:
    lines: list[str] = []
    if log:
        lines.append(f"log: {log}")
    if audit_delivery == "required":
        lines.append("audit_delivery: required")
    if firehose == "metadata":
        lines.append("firehose: metadata")
    lines.append("routes:")
    if not routes:
        lines[-1] = "routes: []"
        return "\n".join(lines) + "\n"
    for r in routes:
        f = _route_to_yaml_fields(r)
        lines.append(f'  - host: "{_yaml_str_escape(str(f["host"]))}"')
        if f.get("inspect") is False:
            lines.append("    inspect: false")
            continue
        inspect: dict[str, object] = f.get("inspect", {})  # type: ignore[assignment]
        if not inspect:
            continue
        lines.append("    inspect:")
        if "auth_scheme" in inspect:
            lines.append(f'      auth_scheme: "{_yaml_str_escape(str(inspect["auth_scheme"]))}"')
        if "auth_header" in inspect:
            lines.append(f'      auth_header: "{_yaml_str_escape(str(inspect["auth_header"]))}"')
        if "token_env" in inspect:
            lines.append(f'      token_env: "{_yaml_str_escape(str(inspect["token_env"]))}"')
        if "matches" in inspect:
            lines.append("      matches:")
            for entry in inspect["matches"]:  # type: ignore[union-attr]
                lines.extend(_render_match_entry(entry))  # type: ignore[arg-type]
        if "git" in inspect:
            git_dict: dict[str, object] = inspect["git"]  # type: ignore
            lines.append("      git:")
            if git_dict.get("fetch") is True:
                lines.append("        fetch: true")
        if inspect.get("preserve_auth") is True:
            lines.append("      preserve_auth: true")
        for dk in ("outbound_detectors", "inbound_detectors", "outbound_on_match"):
            if dk in inspect:
                dv = inspect[dk]
                if dv is False:
                    lines.append(f"      {dk}: false")
                elif isinstance(dv, list):
                    items_str = ", ".join(f'"{x}"' for x in dv)
                    lines.append(f"      {dk}: [{items_str}]")
                elif isinstance(dv, str):
                    lines.append(f'      {dk}: "{_yaml_str_escape(dv)}"')
    return "\n".join(lines) + "\n"


def egress_resolve_token_values(
    token_env_map: dict[str, str],
    source: SecretSource,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for token_env, token_ref in token_env_map.items():
        value = source.get(token_ref)
        if value is None:
            raise MissingEnvVarError(
                token_ref,
                f"egress: secret '{token_ref}' is unset. Add it to the secrets "
                f"file before launching (a route's `value: secret:{token_ref}` "
                f"reads it there), or remove the corresponding auth block from "
                f"bottle.egress.routes.",
            )
        if not value:
            raise MissingEnvVarError(
                token_ref,
                f"egress: secret '{token_ref}' is empty. The "
                f"egress will not inject an empty token; set it to "
                f"the real value or remove the route's auth block.",
            )
        out[token_env] = value
    return out


class Egress:
    """The stateless egress service: `prepare` at launch, then
    `resolve_token_values` and `agent_env_entries`."""

    def prepare(
        self,
        bottle: ManifestBottle,
        slug: str,
        stage_dir: Path,
        provider_routes: tuple[EgressRoute, ...] = (),
        default_audit_delivery: str = "best_effort",
        owner: str = "",
    ) -> EgressPlan:
        routes = egress_routes_for_bottle(bottle, provider_routes, owner)
        log = bottle.egress.Log
        if log >= LOG_FULL:
            cap = full_log.MAX_BYTES * (full_log.BACKUPS + 1) // (1024 * 1024)
            warn(f"bottle {slug}: egress log: 2 records full request and response bodies "
                 f"(each cut at {full_log.BODY_LIMIT_BYTES // 1024} KiB) in the gateway, in "
                 f"{full_log.DEFAULT_DIR}/{full_log.FILE_NAME} (owner-only, rotated, {cap} MiB "
                 "at most). It is verbose and holds whole conversations; use log: 1 "
                 "unless you are debugging.")
        audit_delivery = bottle.audit.delivery or default_audit_delivery
        firehose = bottle.audit.firehose or "full"
        routes_path = stage_dir / EGRESS_ROUTES_FILENAME
        routes_path.write_text(egress_render_routes(
            routes, log=log, audit_delivery=audit_delivery, firehose=firehose))
        routes_path.chmod(0o600)
        # A per-session fake secret under a random env name: the gateway treats
        # it as sensitive and the agent gets it as bait.
        canary = secrets.token_urlsafe(32)
        return EgressPlan(
            slug=slug,
            routes_path=routes_path,
            routes=routes,
            token_env_map=egress_token_env_map(routes),
            log=log,
            canary=canary,
            canary_env=_random_canary_env(),
            audit_delivery=audit_delivery,
            firehose=firehose,
        )

    def resolve_token_values(
        self, token_env_map: dict[str, str], source: SecretSource,
    ) -> dict[str, str]:
        """Each route's upstream credential from `source`. Raises
        `MissingEnvVarError` for an unset or empty secret."""
        return egress_resolve_token_values(token_env_map, source)

    def agent_env_entries(self, plan: EgressPlan) -> tuple[str, ...]:
        """The agent-visible egress env entries (the canary)."""
        return egress_agent_env_entries(plan)
