"""Per-bottle egress proxy (PRD 0017).

Replaces the cred-proxy sidecar (PRD 0010) with a mitmproxy-based
sidecar that becomes the agent's `HTTP_PROXY` / `HTTPS_PROXY`. It
owns three jobs:

  1. MITM the agent's HTTPS with the per-bottle CA (moved from
     pipelock).
  2. Enforce manifest-declared `path_allowlist` per route.
  3. Inject `Authorization` headers for routes that declare an
     `auth` block, the same way cred-proxy does today.

This module defines the abstract proxy (`EgressProxy`), its plan
dataclass (`EgressProxyPlan`), and the resolved per-route shape
(`EgressProxyRoute`). The sidecar's start/stop lifecycle is backend-
specific and lives on concrete subclasses (see
`claude_bottle/backend/docker/egress_proxy.py`).

Chunks 1+2 of the PRD: this module + the mitmproxy addon + the Docker
lifecycle are wired into the agent's `HTTP_PROXY` path; cred-proxy
has been removed. Chunk 3 retargets the cred-proxy-block remediation
flow (PRD 0014) at egress-proxy and renames the MCP tool.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .log import die
from .manifest import Bottle


# DNS name agents will dial for the per-bottle egress-proxy sidecar.
# Backend-agnostic by contract: every concrete backend (Docker today,
# others later) attaches this name to its sidecar on the bottle's
# internal network. The agent's `HTTP_PROXY` env var resolves to
# `http://egress-proxy:<port>` once chunk 2 cuts over.
EGRESS_PROXY_HOSTNAME = "egress-proxy"

# In-container path the addon reads. Pre-created in
# `Dockerfile.egress-proxy` so `docker cp` can drop the file directly.
# `.yaml` extension per PRD 0017 — content is JSON (valid YAML) so
# both sides can use stdlib `json`.
EGRESS_PROXY_ROUTES_IN_CONTAINER = "/etc/egress-proxy/routes.yaml"


@dataclass(frozen=True)
class EgressProxyRoute:
    """One resolved route on the egress-proxy sidecar.

    `host` matches the request's hostname (case-insensitive). The
    optional `path_allowlist` constrains the URL path; empty tuple
    means no path-level filtering. The `auth_scheme` / `token_env` /
    `token_ref` triple is the credential-injection config; empty
    strings mean "no auth injection" (the manifest's nested `auth`
    block was omitted).

    `token_env` is the env-var slot inside the egress-proxy container
    (e.g. `EGRESS_PROXY_TOKEN_0`); `token_ref` is the host env var
    the CLI reads at launch and forwards into the container's environ
    under `token_env`. Routes that share a `token_ref` coalesce to
    one `token_env` slot."""

    host: str
    path_allowlist: tuple[str, ...] = ()
    auth_scheme: str = ""
    token_env: str = ""
    token_ref: str = ""


@dataclass(frozen=True)
class EgressProxyPlan:
    """Output of EgressProxy.prepare; consumed by .start.

    The slug + routes_path + routes + token_env_map fields are
    filled at prepare time (host-side, side-effect-free on docker).
    The network + CA + pipelock fields are populated by the backend's
    launch step via `dataclasses.replace` once those resources
    exist. Empty defaults are sentinels meaning "not yet set";
    `.start` validates that they are populated.

    `token_env_map` is `{<token_env in container>: <token_ref on host>}`.
    The backend's start step reads `os.environ[token_ref]` and
    forwards the value into the egress-proxy container's environ
    under `token_env`. The plan itself never holds token values —
    secrets never land in a dataclass that might be logged.

    `mitmproxy_ca_host_path` is the host path of the per-bottle
    egress-proxy CA (single PEM with cert+key concatenated) minted
    by `egress_proxy_tls_init`. `.start` docker-cps it into the
    sidecar at `~/.mitmproxy/mitmproxy-ca.pem` — mitmproxy reads
    that file at boot to mint per-host leaf certs.

    `mitmproxy_ca_cert_only_host_path` is the cert-only PEM (no
    key) for installing into the agent's trust store via
    `provision_ca`. Separate file rather than re-parsing the
    concat so secrets and trust artefacts stay on distinct paths.

    `pipelock_ca_host_path` is the host path of the pipelock CA
    (cert only). `.start` docker-cps it into the sidecar so the
    proxy's outbound HTTPS client trusts pipelock's MITM on the
    egress-proxy → upstream leg.

    `pipelock_proxy_url` is the URL egress-proxy sets as `HTTPS_PROXY`
    in its environ so outbound HTTPS traverses pipelock — keeping
    pipelock's hostname allowlist + DLP body scanner on the
    egress-proxy → upstream leg.
    """

    slug: str
    routes_path: Path
    routes: tuple[EgressProxyRoute, ...]
    token_env_map: dict[str, str]
    internal_network: str = ""
    egress_network: str = ""
    mitmproxy_ca_host_path: Path = Path()
    mitmproxy_ca_cert_only_host_path: Path = Path()
    pipelock_ca_host_path: Path = Path()
    pipelock_proxy_url: str = ""


def egress_proxy_routes_for_bottle(
    bottle: Bottle,
) -> tuple[EgressProxyRoute, ...]:
    """Lift each `bottle.egress_proxy.routes[]` manifest entry into a
    resolved EgressProxyRoute. Order is preserved so route lookup at
    the proxy is stable.

    Token-env slots are assigned per distinct `token_ref`: the first
    authenticated route with `token_ref` "GH_PAT" gets
    `EGRESS_PROXY_TOKEN_0`; a second route with the same `token_ref`
    shares slot 0. Unauthenticated routes (`auth` omitted) contribute
    no slot."""
    out: list[EgressProxyRoute] = []
    slot_for_token: dict[str, str] = {}
    for r in bottle.egress_proxy.routes:
        if r.AuthScheme and r.TokenRef:
            token_env = slot_for_token.get(r.TokenRef)
            if token_env is None:
                token_env = f"EGRESS_PROXY_TOKEN_{len(slot_for_token)}"
                slot_for_token[r.TokenRef] = token_env
            out.append(EgressProxyRoute(
                host=r.Host,
                path_allowlist=r.PathAllowlist,
                auth_scheme=r.AuthScheme,
                token_env=token_env,
                token_ref=r.TokenRef,
            ))
        else:
            out.append(EgressProxyRoute(
                host=r.Host,
                path_allowlist=r.PathAllowlist,
            ))
    return tuple(out)


def egress_proxy_token_env_map(
    routes: tuple[EgressProxyRoute, ...],
) -> dict[str, str]:
    """Collapse the route list into `{token_env: token_ref}` for the
    authenticated routes. Routes without `auth` contribute no entry.

    Conflict detection: two routes that share a `token_env` slot but
    name different `token_ref` host vars is a programming error in
    `egress_proxy_routes_for_bottle`; surface it as a die rather than
    silently picking one."""
    out: dict[str, str] = {}
    for r in routes:
        if not r.token_env:
            continue
        existing = out.get(r.token_env)
        if existing is not None and existing != r.token_ref:
            die(
                f"egress-proxy plan conflict: {r.token_env} maps to both "
                f"{existing!r} and {r.token_ref!r}. Two routes sharing a "
                f"token slot must reference the same host env var."
            )
        out[r.token_env] = r.token_ref
    return out


def egress_proxy_render_routes(
    routes: tuple[EgressProxyRoute, ...],
) -> str:
    """Serialize the route table for the addon to read.

    JSON content (valid YAML), no token values, no host env-var
    names — the only thing the addon needs at runtime is the host →
    path_allowlist + auth_scheme + in-container env-var mapping. The
    actual token values arrive via the container's environ.

    Authenticated routes carry `auth_scheme` + `token_env`;
    unauthenticated routes omit both keys (the addon's parser
    enforces both-or-neither)."""
    payload_routes: list[dict[str, object]] = []
    for r in routes:
        entry: dict[str, object] = {"host": r.host}
        if r.path_allowlist:
            entry["path_allowlist"] = list(r.path_allowlist)
        if r.auth_scheme and r.token_env:
            entry["auth_scheme"] = r.auth_scheme
            entry["token_env"] = r.token_env
        payload_routes.append(entry)
    payload = {"routes": payload_routes}
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def egress_proxy_resolve_token_values(
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
                f"egress-proxy: host env var '{token_ref}' is unset. Set it "
                f"before launching, or remove the corresponding auth block "
                f"from bottle.egress_proxy.routes."
            )
        if not value:
            die(
                f"egress-proxy: host env var '{token_ref}' is empty. The "
                f"egress-proxy will not inject an empty token; set it to "
                f"the real value or remove the route's auth block."
            )
        out[token_env] = value
    return out


class EgressProxy(ABC):
    """The per-bottle egress proxy. Encapsulates the host-side prepare
    (route lift + routes.yaml render + token-env-map derivation); the
    sidecar's start/stop lifecycle is backend-specific and lives on
    concrete subclasses."""

    def prepare(self, bottle: Bottle, slug: str, stage_dir: Path) -> EgressProxyPlan:
        """Lift `bottle.egress_proxy.routes` into resolved routes,
        render the routes file (mode 600) under `stage_dir`, and
        return the plan. Pure host-side, no docker subprocess. The
        token-env map records the mapping the launch step uses to
        forward values from the host's environ into the sidecar's
        environ.

        Returned plan is incomplete: the launch step must fill
        `internal_network` / `egress_network` / `pipelock_proxy_url`
        via `dataclasses.replace` before passing it to `.start`."""
        routes = egress_proxy_routes_for_bottle(bottle)
        routes_path = stage_dir / "egress_proxy_routes.yaml"
        routes_path.write_text(egress_proxy_render_routes(routes))
        routes_path.chmod(0o600)
        return EgressProxyPlan(
            slug=slug,
            routes_path=routes_path,
            routes=routes,
            token_env_map=egress_proxy_token_env_map(routes),
        )

    @abstractmethod
    def start(self, plan: EgressProxyPlan) -> str:
        """Bring up the egress-proxy sidecar according to `plan`.
        Returns the target string identifying the running instance —
        the same value to pass to `.stop`. Backend-specific."""

    @abstractmethod
    def stop(self, target: str) -> None:
        """Tear down the egress-proxy sidecar identified by `target`
        (the value `.start` returned). Idempotent: a missing target
        is success. Backend-specific."""


__all__ = [
    "EGRESS_PROXY_HOSTNAME",
    "EGRESS_PROXY_ROUTES_IN_CONTAINER",
    "EgressProxy",
    "EgressProxyPlan",
    "EgressProxyRoute",
    "egress_proxy_render_routes",
    "egress_proxy_resolve_token_values",
    "egress_proxy_routes_for_bottle",
    "egress_proxy_token_env_map",
]
