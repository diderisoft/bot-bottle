"""Per-bottle egress proxy (PRD 0017).

Replaces the cred-proxy sidecar (PRD 0010) with a mitmproxy-based
sidecar that becomes the agent's `HTTP_PROXY` / `HTTPS_PROXY`. It
owns three jobs:

  1. MITM the agent's HTTPS with the per-bottle CA (moved from
     pipelock).
  2. Enforce manifest-declared `path_allowlist` per route.
  3. Inject `Authorization` headers for routes that declare an
     `auth` block, the same way cred-proxy does today.

This module defines the abstract proxy (`Egress`), its plan
dataclass (`EgressPlan`), and the resolved per-route shape
(`EgressRoute`). The sidecar's start/stop lifecycle is backend-
specific and lives on concrete subclasses (see
`claude_bottle/backend/docker/egress.py`).

Chunks 1+2 of the PRD: this module + the mitmproxy addon + the Docker
lifecycle are wired into the agent's `HTTP_PROXY` path; cred-proxy
has been removed. Chunk 3 retargets the cred-proxy-block remediation
flow (PRD 0014) at egress and renames the MCP tool.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .log import die
from .manifest import Bottle


# DNS name agents will dial for the per-bottle egress sidecar.
# Backend-agnostic by contract: every concrete backend (Docker today,
# others later) attaches this name to its sidecar on the bottle's
# internal network. The agent's `HTTP_PROXY` env var resolves to
# `http://egress:<port>` once chunk 2 cuts over.
EGRESS_HOSTNAME = "egress"

# In-container path the addon reads. Pre-created in
# `Dockerfile.sidecars` so the host bind-mount can drop the file
# directly. Content is YAML (hand-rolled by `egress_render_routes`
# in the style of `pipelock_render_yaml`, parsed by `yaml_subset`
# inside the addon).
EGRESS_ROUTES_IN_CONTAINER = "/etc/egress/routes.yaml"


@dataclass(frozen=True)
class EgressRoute:
    """One resolved route on the egress sidecar.

    `host` matches the request's hostname (case-insensitive). The
    optional `path_allowlist` constrains the URL path; empty tuple
    means no path-level filtering. The `auth_scheme` / `token_env` /
    `token_ref` triple is the credential-injection config; empty
    strings mean "no auth injection" (the manifest's nested `auth`
    block was omitted).

    `token_env` is the env-var slot inside the egress container
    (e.g. `EGRESS_TOKEN_0`); `token_ref` is the host env var
    the CLI reads at launch and forwards into the container's environ
    under `token_env`. Routes that share a `token_ref` coalesce to
    one `token_env` slot.

    `roles` carries the manifest route's optional role markers (see
    `manifest.EGRESS_ROLES`). The launch step reads these for
    side effects like the claude-code OAuth placeholder env."""

    host: str
    path_allowlist: tuple[str, ...] = ()
    auth_scheme: str = ""
    token_env: str = ""
    token_ref: str = ""
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class EgressPlan:
    """Output of Egress.prepare; consumed by .start.

    The slug + routes_path + routes + token_env_map fields are
    filled at prepare time (host-side, side-effect-free on docker).
    The network + CA + pipelock fields are populated by the backend's
    launch step via `dataclasses.replace` once those resources
    exist. Empty defaults are sentinels meaning "not yet set";
    `.start` validates that they are populated.

    `token_env_map` is `{<token_env in container>: <token_ref on host>}`.
    The backend's start step reads `os.environ[token_ref]` and
    forwards the value into the egress container's environ
    under `token_env`. The plan itself never holds token values —
    secrets never land in a dataclass that might be logged.

    `mitmproxy_ca_host_path` is the host path of the per-bottle
    egress CA (single PEM with cert+key concatenated) minted
    by `egress_tls_init`. `.start` docker-cps it into the
    sidecar at `~/.mitmproxy/mitmproxy-ca.pem` — mitmproxy reads
    that file at boot to mint per-host leaf certs.

    `mitmproxy_ca_cert_only_host_path` is the cert-only PEM (no
    key) for installing into the agent's trust store via
    `provision_ca`. Separate file rather than re-parsing the
    concat so secrets and trust artefacts stay on distinct paths.

    `pipelock_ca_host_path` is the host path of the pipelock CA
    (cert only). `.start` docker-cps it into the sidecar so the
    proxy's outbound HTTPS client trusts pipelock's MITM on the
    egress → upstream leg.

    `pipelock_proxy_url` is the URL egress sets as `HTTPS_PROXY`
    in its environ so outbound HTTPS traverses pipelock — keeping
    pipelock's hostname allowlist + DLP body scanner on the
    egress → upstream leg.
    """

    slug: str
    routes_path: Path
    routes: tuple[EgressRoute, ...]
    token_env_map: dict[str, str]
    internal_network: str = ""
    egress_network: str = ""
    mitmproxy_ca_host_path: Path = Path()
    mitmproxy_ca_cert_only_host_path: Path = Path()
    pipelock_ca_host_path: Path = Path()
    pipelock_proxy_url: str = ""


# Hosts the agent needs by default for claude-code itself. Folded
# into every bottle's egress routes table as bare-pass entries
# (no auth, no path filter) so the agent reaches them without each
# bottle having to opt in. Pipelock used to own this list; PRD 0017
# moves it to egress because egress is the primary gate
# now and pipelock's allowlist is mirrored from egress.
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "api.anthropic.com",
    "statsig.anthropic.com",
    "sentry.io",
    "claude.ai",
    "platform.claude.com",
    "downloads.claude.ai",
    "raw.githubusercontent.com",
)


def egress_manifest_routes(
    bottle: Bottle,
) -> tuple[EgressRoute, ...]:
    """Lift each `bottle.egress.routes[]` manifest entry into a
    resolved EgressRoute. Order is preserved so route lookup at
    the proxy is stable.

    Token-env slots are assigned per distinct `token_ref`: the first
    authenticated route with `token_ref` "GH_PAT" gets
    `EGRESS_TOKEN_0`; a second route with the same `token_ref`
    shares slot 0. Unauthenticated routes (`auth` omitted) contribute
    no slot.

    Does NOT include the folded-in DEFAULT_ALLOWLIST /
    bottle.egress.allowlist bare-pass entries — see
    `egress_routes_for_bottle` for the effective set the
    addon enforces."""
    out: list[EgressRoute] = []
    slot_for_token: dict[str, str] = {}
    for r in bottle.egress.routes:
        if r.AuthScheme and r.TokenRef:
            token_env = slot_for_token.get(r.TokenRef)
            if token_env is None:
                token_env = f"EGRESS_TOKEN_{len(slot_for_token)}"
                slot_for_token[r.TokenRef] = token_env
            out.append(EgressRoute(
                host=r.Host,
                path_allowlist=r.PathAllowlist,
                auth_scheme=r.AuthScheme,
                token_env=token_env,
                token_ref=r.TokenRef,
                roles=r.Role,
            ))
        else:
            out.append(EgressRoute(
                host=r.Host,
                path_allowlist=r.PathAllowlist,
                roles=r.Role,
            ))
    return tuple(out)


def egress_routes_for_bottle(
    bottle: Bottle,
) -> tuple[EgressRoute, ...]:
    """Effective egress routes: manifest routes followed by
    bare-pass entries for DEFAULT_ALLOWLIST hosts. This is what
    gets rendered into routes.yaml + what the addon enforces.

    Manifest routes win over defaults on host collision (manifest
    routes carry more specific config — auth, path filter, role
    markers). Hostname comparison is case-insensitive.

    Operators that want to allow an arbitrary host that isn't in
    DEFAULT_ALLOWLIST declare it directly in
    `bottle.egress.routes` as a bare-pass entry
    (`- host: <name>`). The legacy `bottle.egress.allowlist`
    folding is gone — egress is the single allowlist surface."""
    out: list[EgressRoute] = list(egress_manifest_routes(bottle))
    claimed: set[str] = {r.host.lower() for r in out}
    for host in DEFAULT_ALLOWLIST:
        if host.lower() not in claimed:
            out.append(EgressRoute(host=host))
            claimed.add(host.lower())
    return tuple(out)


def egress_token_env_map(
    routes: tuple[EgressRoute, ...],
) -> dict[str, str]:
    """Collapse the route list into `{token_env: token_ref}` for the
    authenticated routes. Routes without `auth` contribute no entry.

    Conflict detection: two routes that share a `token_env` slot but
    name different `token_ref` host vars is a programming error in
    `egress_routes_for_bottle`; surface it as a die rather than
    silently picking one."""
    out: dict[str, str] = {}
    for r in routes:
        if not r.token_env:
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


def egress_render_routes(
    routes: tuple[EgressRoute, ...],
) -> str:
    """Serialize the route table for the addon to read.

    YAML content — no token values, no host env-var names. The only
    thing the addon needs at runtime is the host → path_allowlist
    + auth_scheme + in-container env-var mapping. The actual token
    values arrive via the container's environ.

    Authenticated routes carry `auth_scheme` + `token_env`;
    unauthenticated routes omit both keys (the addon's parser
    enforces both-or-neither). Hand-rolled YAML in the style of
    `pipelock_render_yaml` so the addon's parser
    (`yaml_subset.parse_yaml_subset`) round-trips it cleanly."""
    lines: list[str] = ["routes:"]
    if not routes:
        # `routes:` with an empty list on the same line — the parser
        # needs SOMETHING here. Empty inline list is the cleanest.
        lines[0] = "routes: []"
        return "\n".join(lines) + "\n"
    for r in routes:
        lines.append(f'  - host: "{r.host}"')
        if r.auth_scheme and r.token_env:
            lines.append(f'    auth_scheme: "{r.auth_scheme}"')
            lines.append(f'    token_env: "{r.token_env}"')
        if r.path_allowlist:
            lines.append("    path_allowlist:")
            for p in r.path_allowlist:
                lines.append(f'      - "{p}"')
    return "\n".join(lines) + "\n"


def egress_resolve_token_values(
    token_env_map: dict[str, str],
    host_env: dict[str, str],
) -> dict[str, str]:
    """Read `host_env[TokenRef]` for each entry in `token_env_map` and
    return `{token_env: <value>}`. Dies (with a pointer at the missing
    var name) if any TokenRef is unset.

    Pure function: takes the host env as an argument so tests can pass
    a sealed mapping without touching `os.environ`."""
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
    """The per-bottle egress proxy. Encapsulates the host-side prepare
    (route lift + routes.yaml render + token-env-map derivation); the
    sidecar's start/stop lifecycle is backend-specific and lives on
    concrete subclasses."""

    def prepare(self, bottle: Bottle, slug: str, stage_dir: Path) -> EgressPlan:
        """Lift `bottle.egress.routes` into resolved routes,
        render the routes file (mode 600) under `stage_dir`, and
        return the plan. Pure host-side, no docker subprocess. The
        token-env map records the mapping the launch step uses to
        forward values from the host's environ into the sidecar's
        environ.

        Returned plan is incomplete: the launch step must fill
        `internal_network` / `egress_network` / `pipelock_proxy_url`
        via `dataclasses.replace` before passing it to `.start`."""
        routes = egress_routes_for_bottle(bottle)
        routes_path = stage_dir / "egress_routes.yaml"
        routes_path.write_text(egress_render_routes(routes))
        routes_path.chmod(0o600)
        return EgressPlan(
            slug=slug,
            routes_path=routes_path,
            routes=routes,
            token_env_map=egress_token_env_map(routes),
        )

__all__ = [
    "DEFAULT_ALLOWLIST",
    "EGRESS_HOSTNAME",
    "EGRESS_ROUTES_IN_CONTAINER",
    "Egress",
    "EgressPlan",
    "EgressRoute",
    "egress_manifest_routes",
    "egress_render_routes",
    "egress_resolve_token_values",
    "egress_routes_for_bottle",
    "egress_token_env_map",
]
