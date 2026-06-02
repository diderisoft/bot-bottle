"""Manifest dataclasses (PRD 0011 layout).

Reads the per-file manifest tree:

  $HOME/.bot-bottle/bottles/<name>.md   — one bottle per file
  $HOME/.bot-bottle/agents/<name>.md    — home-resident agents
  $CWD/.bot-bottle/agents/<name>.md     — cwd-supplied agents

Each file is Markdown with YAML frontmatter. The frontmatter holds
the structured config (see schema below); for agents the body is
the system prompt, for bottles the body is human documentation
(ignored by the parser).

Bottle schema (frontmatter):
  extends:      <bottle-name>                   # optional (PRD 0025)
  env:          { <NAME>: <env-entry>, ... }
  git:
    user:       { name: <str>, email: <str> }   # optional
    remotes:    { <host>: <git-entry>, ... }    # optional
  egress: { routes: [ <egress-route>, ... ] }
    # route keys: host, path_allowlist, auth, role, pipelock
    # pipelock: { tls_passthrough: <bool>, ssrf_ip_allowlist: [<cidr>, ...] }
  supervise:    <bool>                          # optional

Agent schema (frontmatter):
  bottle:        <bottle-name>          # required
  skills:        [ <skill-name>, ... ]   # optional
  # Claude Code subagent passthrough fields — accepted, ignored:
  name, description, model, color, memory

The agent file's Markdown body is the system prompt (stripped).
Unknown top-level frontmatter keys raise ManifestError with a hint.

Bottles can ONLY live under $HOME. A bottles/ dir under $CWD is a
warn at load time and contributes nothing. The trust boundary is
expressed as filesystem layout rather than resolver logic.

Validation runs once at load. Manifest.from_json_obj is preserved
as a programmatic entry point (used by tests) that takes a dict
with the same field names — useful for building manifests without
on-disk files.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, cast

from .agent_provider import PROVIDER_TEMPLATES
from .log import warn
from .yaml_subset import YamlSubsetError, parse_frontmatter


class ManifestError(Exception):
    """A manifest file (or the manifest tree) is invalid."""


def _empty_str_dict() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class GitEntry:
    """One upstream the per-agent git-gate (PRD 0008) is allowed to
    talk to. `Upstream` is the real remote URL the agent would push to
    if there were no gate; the gate hosts a bare repo at /git/<Name>.git
    and `IdentityFile` is the SSH key the gate uses to push that repo
    upstream after gitleaks passes. The agent itself never holds the
    upstream credential.

    `ExtraHosts` is an optional `{hostname: ip}` map injected into the
    gate container's `/etc/hosts` via `--add-host`. Use it when the
    Upstream's hostname isn't resolvable from the gate (e.g. a
    Tailscale-only host whose public DNS A record points elsewhere):
    the agent's `insteadOf` rewrite still matches the original
    hostname, but the gate routes to the right IP.

    The Upstream URL is parsed once at construction and the pieces are
    stashed in the `Upstream*` fields so the git-gate render step
    doesn't have to re-parse."""

    Name: str
    Upstream: str
    IdentityFile: str
    KnownHostKey: str = ""
    ExtraHosts: Mapping[str, str] = field(default_factory=_empty_str_dict)
    RemoteKey: str = ""
    UpstreamUser: str = ""
    UpstreamHost: str = ""
    UpstreamPort: str = ""
    UpstreamPath: str = ""

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "GitEntry":
        d = _as_json_object(raw, f"bottle '{bottle_name}' git[{idx}]")
        return cls._from_object(bottle_name, d, f"git[{idx}]", None)

    @classmethod
    def from_remote_dict(
        cls, bottle_name: str, host_key: str, raw: object
    ) -> "GitEntry":
        if not host_key:
            raise ManifestError(f"bottle '{bottle_name}' git.remotes has an empty host key")
        d = _as_json_object(raw, f"bottle '{bottle_name}' git.remotes[{host_key!r}]")
        return cls._from_object(
            bottle_name, d, f"git.remotes[{host_key!r}]", host_key,
        )

    @classmethod
    def _from_object(
        cls,
        bottle_name: str,
        d: dict[str, object],
        label: str,
        host_key: str | None,
    ) -> "GitEntry":
        name = d.get("Name")
        if not isinstance(name, str) or not name:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} missing required string "
                f"field 'Name'"
            )
        upstream = d.get("Upstream")
        if not isinstance(upstream, str) or not upstream:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} '{name}' missing required string field "
                f"'Upstream'"
            )
        ident = d.get("IdentityFile")
        if not isinstance(ident, str) or not ident:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} '{name}' missing required string field "
                f"'IdentityFile'"
            )
        khk = _opt_str(
            d.get("KnownHostKey"),
            f"bottle '{bottle_name}' {label} '{name}' KnownHostKey",
        )
        extra_hosts = _opt_extra_hosts(
            d.get("ExtraHosts"),
            f"bottle '{bottle_name}' {label} '{name}' ExtraHosts",
        )
        user, host, port, path = _parse_git_upstream(
            upstream, f"bottle '{bottle_name}' {label} '{name}' Upstream"
        )
        if (
            host_key is not None
            and host_key != host
            and not _is_ip_literal(host)
        ):
            raise ManifestError(
                f"bottle '{bottle_name}' git.remotes key {host_key!r} "
                f"does not match Upstream host {host!r}"
            )
        return cls(
            Name=name,
            Upstream=upstream,
            IdentityFile=ident,
            KnownHostKey=khk,
            ExtraHosts=extra_hosts,
            RemoteKey=host_key or host,
            UpstreamUser=user,
            UpstreamHost=host,
            UpstreamPort=port,
            UpstreamPath=path,
        )


# Auth schemes for the egress route's optional `auth` block.
# Same values cred-proxy accepts today; `token` sidesteps the Gitea
# token-not-Bearer quirk (go-gitea/gitea#16734).
EGRESS_AUTH_SCHEMES = ("Bearer", "token")


@dataclass(frozen=True)
class AgentProvider:
    """Provider/template for the agent process inside a bottle.

    `template` selects a built-in launch/runtime contract. `dockerfile`
    optionally points at a custom agent-image Dockerfile while leaving
    bot-bottle's sidecar infrastructure intact.

    `auth_token` names the host env var that holds the provider's OAuth
    token (Claude only). The provisioner injects a provider-owned egress
    route for api.anthropic.com that re-injects this token as the Bearer
    header, and sets a placeholder CLAUDE_CODE_OAUTH_TOKEN in the agent
    so the Claude Code CLI starts.

    `forward_host_credentials` forwards the host Codex auth token into
    the egress sidecar (Codex only).
    """

    template: str = "claude"
    dockerfile: str = ""
    auth_token: str = ""
    forward_host_credentials: bool = False

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "AgentProvider":
        d = _as_json_object(raw, f"bottle '{bottle_name}' agent_provider")
        for k in d:
            if k not in {"template", "dockerfile", "auth_token", "forward_host_credentials"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' agent_provider has unknown key {k!r}; "
                    f"allowed: template, dockerfile, auth_token, forward_host_credentials"
                )
        template = d.get("template", "claude")
        if not isinstance(template, str) or not template:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.template must be a "
                f"non-empty string"
            )
        if template not in PROVIDER_TEMPLATES:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.template {template!r} "
                f"is not one of {', '.join(sorted(PROVIDER_TEMPLATES))}"
            )
        dockerfile = d.get("dockerfile", "")
        if not isinstance(dockerfile, str):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.dockerfile must be a "
                f"string (was {type(dockerfile).__name__})"
            )
        auth_token = d.get("auth_token", "")
        if not isinstance(auth_token, str):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.auth_token must be a "
                f"string (was {type(auth_token).__name__})"
            )
        if auth_token and template != "claude":
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.auth_token is only "
                f"supported for template 'claude'"
            )
        forward_host_credentials = d.get("forward_host_credentials", False)
        if not isinstance(forward_host_credentials, bool):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                f"must be a boolean (was {type(forward_host_credentials).__name__})"
            )
        if forward_host_credentials and template != "codex":
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                "is currently only supported for template 'codex'"
            )
        return cls(
            template=template,
            dockerfile=dockerfile,
            auth_token=auth_token,
            forward_host_credentials=forward_host_credentials,
        )


@dataclass(frozen=True)
class GitUser:
    """Per-bottle `git config --global user.name` / `user.email`
    pair (issue #86). The agent's commits inside the bottle are
    attributed to this identity rather than the agent image's
    image-baked default (no user, or whatever the image dropped
    in). Either or both fields can be set independently.

    `from_dict` is forgiving on shape (a single missing field is
    fine — we just skip that config line at provisioning) but
    strict on types (string-or-die)."""

    name: str = ""
    email: str = ""

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "GitUser":
        d = _as_json_object(raw, f"bottle '{bottle_name}' git.user")
        for k in d.keys():
            if k not in {"name", "email"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' git.user has unknown key {k!r}; "
                    f"allowed: name, email"
                )
        name = d.get("name", "")
        email = d.get("email", "")
        if not isinstance(name, str):
            raise ManifestError(
                f"bottle '{bottle_name}' git.user.name must be a string "
                f"(was {type(name).__name__})"
            )
        if not isinstance(email, str):
            raise ManifestError(
                f"bottle '{bottle_name}' git.user.email must be a string "
                f"(was {type(email).__name__})"
            )
        if not name and not email:
            raise ManifestError(
                f"bottle '{bottle_name}' git.user is set but neither "
                f"name nor email is non-empty; remove the block or "
                f"fill at least one field."
            )
        return cls(name=name, email=email)

    def is_empty(self) -> bool:
        return not self.name and not self.email


def _parse_git_config(
    bottle_name: str,
    raw: object,
) -> tuple[tuple[GitEntry, ...], GitUser]:
    d = _as_json_object(raw, f"bottle '{bottle_name}' git")
    for k in d.keys():
        if k not in {"user", "remotes"}:
            raise ManifestError(
                f"bottle '{bottle_name}' git has unknown key {k!r}; "
                f"allowed: user, remotes"
            )

    git_user = (
        GitUser.from_dict(bottle_name, d["user"])
        if "user" in d
        else GitUser()
    )

    git: tuple[GitEntry, ...] = ()
    remotes_raw = d.get("remotes")
    if remotes_raw is not None:
        remotes = _as_json_object(remotes_raw, f"bottle '{bottle_name}' git.remotes")
        git = tuple(
            GitEntry.from_remote_dict(bottle_name, host, entry)
            for host, entry in remotes.items()
        )
        _validate_unique_git_names(bottle_name, git)

    return git, git_user


@dataclass(frozen=True)
class PipelockRoutePolicy:
    """Per-route pipelock policy overrides.

    `TlsPassthrough` adds the route host to pipelock's
    `tls_interception.passthrough_domains`, so pipelock still enforces
    the hostname allowlist but does not MITM/decrypt request bodies or
    headers for that host.

    `SsrfIpAllowlist` adds explicit IPs/CIDRs to pipelock's SSRF
    allowlist for private/internal destinations behind this route.
    """

    TlsPassthrough: bool = False
    SsrfIpAllowlist: tuple[str, ...] = ()

    @classmethod
    def from_dict(
        cls, bottle_name: str, idx: int, raw: object,
    ) -> "PipelockRoutePolicy":
        label = f"bottle '{bottle_name}' egress.routes[{idx}] pipelock"
        d = _as_json_object(raw, label)
        for k in d:
            if k not in ("tls_passthrough", "ssrf_ip_allowlist"):
                raise ManifestError(
                    f"{label} has unknown key {k!r}; "
                    f"only 'tls_passthrough' and 'ssrf_ip_allowlist' "
                    f"are accepted"
                )
        tls_passthrough_raw = d.get("tls_passthrough", False)
        if not isinstance(tls_passthrough_raw, bool):
            raise ManifestError(
                f"{label}.tls_passthrough must be a boolean "
                f"(was {type(tls_passthrough_raw).__name__})"
            )
        ssrf_raw = d.get("ssrf_ip_allowlist", [])
        if not isinstance(ssrf_raw, list):
            raise ManifestError(
                f"{label}.ssrf_ip_allowlist must be an array "
                f"(was {type(ssrf_raw).__name__})"
            )
        ssrf_ip_allowlist: list[str] = []
        for j, item in enumerate(ssrf_raw):
            if not isinstance(item, str) or not item:
                raise ManifestError(
                    f"{label}.ssrf_ip_allowlist[{j}] must be a non-empty "
                    f"string (was {type(item).__name__})"
                )
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as e:
                raise ManifestError(
                    f"{label}.ssrf_ip_allowlist[{j}] must be an IP address "
                    f"or CIDR (was {item!r}): {e}"
                )
            ssrf_ip_allowlist.append(item)
        return cls(
            TlsPassthrough=tls_passthrough_raw,
            SsrfIpAllowlist=tuple(ssrf_ip_allowlist),
        )


@dataclass(frozen=True)
class EgressRoute:
    """One route on the per-bottle egress sidecar (PRD 0017).

    `Host` matches the request's hostname (case-insensitive). The
    optional `PathAllowlist` constrains the URL path to a set of
    prefixes; empty tuple means no path-level filtering. The optional
    `AuthScheme` / `TokenRef` pair drives credential injection:
    when set, the proxy strips any inbound Authorization and injects
    `<AuthScheme> <value-of-host-env-named-by-TokenRef>`. When the
    manifest's `auth` block is omitted both fields are empty strings —
    no Authorization is written, no token forwarded.

    `Role` is reserved for future use; all role strings are currently
    rejected by the validator.

    Validation rules (enforced in `from_dict`):
      - `host` required, non-empty.
      - `path_allowlist` optional, list of absolute path prefixes.
      - `auth` optional. If present, MUST carry both `scheme` and
        `token_ref` as non-empty strings; an empty `auth: {}` is an
        error rather than a synonym for "no auth" (omit `auth` for
        that case).
      - `role` optional, reserved — any non-empty value is rejected.
    """

    Host: str
    PathAllowlist: tuple[str, ...] = ()
    AuthScheme: str = ""
    TokenRef: str = ""
    Role: tuple[str, ...] = ()
    Pipelock: PipelockRoutePolicy = field(default_factory=PipelockRoutePolicy)

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "EgressRoute":
        label = f"bottle '{bottle_name}' egress.routes[{idx}]"
        d = _as_json_object(raw, label)
        host = d.get("host")
        if not isinstance(host, str) or not host:
            raise ManifestError(f"{label} missing required string field 'host'")

        path_allow_raw = d.get("path_allowlist")
        prefixes: tuple[str, ...] = ()
        if path_allow_raw is not None:
            if not isinstance(path_allow_raw, list):
                raise ManifestError(
                    f"{label} path_allowlist must be an array "
                    f"(was {type(path_allow_raw).__name__})"
                )
            path_list = cast(list[object], path_allow_raw)
            collected: list[str] = []
            for j, p in enumerate(path_list):
                if not isinstance(p, str):
                    raise ManifestError(
                        f"{label} path_allowlist[{j}] must be a string "
                        f"(was {type(p).__name__})"
                    )
                if not p.startswith("/"):
                    raise ManifestError(
                        f"{label} path_allowlist[{j}] {p!r} must be an "
                        f"absolute path prefix starting with '/'"
                    )
                collected.append(p)
            prefixes = tuple(collected)

        auth_scheme = ""
        token_ref = ""
        if "auth" in d:
            auth_raw = d.get("auth")
            auth_d = _as_json_object(auth_raw, f"{label} auth")
            if not auth_d:
                raise ManifestError(
                    f"{label} auth is empty ({{}}); omit the 'auth' key "
                    f"entirely if this route is unauthenticated. Otherwise "
                    f"both 'scheme' and 'token_ref' are required."
                )
            auth_scheme_raw = auth_d.get("scheme")
            if not isinstance(auth_scheme_raw, str) or not auth_scheme_raw:
                raise ManifestError(
                    f"{label} auth.scheme is required when 'auth' is set "
                    f"(non-empty string)"
                )
            if auth_scheme_raw not in EGRESS_AUTH_SCHEMES:
                raise ManifestError(
                    f"{label} auth.scheme {auth_scheme_raw!r} is not one of "
                    f"{', '.join(EGRESS_AUTH_SCHEMES)}"
                )
            token_ref_raw = auth_d.get("token_ref")
            if not isinstance(token_ref_raw, str) or not token_ref_raw:
                raise ManifestError(
                    f"{label} auth.token_ref is required when 'auth' is set "
                    f"(name of the host env var holding the token value)"
                )
            for k in auth_d:
                if k not in ("scheme", "token_ref"):
                    raise ManifestError(
                        f"{label} auth has unknown key {k!r}; "
                        f"only 'scheme' and 'token_ref' are accepted"
                    )
            auth_scheme = auth_scheme_raw
            token_ref = token_ref_raw

        role_raw = d.get("role")
        roles: tuple[str, ...] = ()
        if role_raw is None:
            roles = ()
        elif isinstance(role_raw, str):
            roles = (role_raw,)
        elif isinstance(role_raw, list):
            role_list = cast(list[object], role_raw)
            collected_roles: list[str] = []
            for r in role_list:
                if not isinstance(r, str):
                    raise ManifestError(f"{label} role items must be strings (got {type(r).__name__})")
                collected_roles.append(r)
            roles = tuple(collected_roles)
        else:
            raise ManifestError(
                f"{label} role must be a string or a list of strings "
                f"(was {type(role_raw).__name__})"
            )
        if roles:
            raise ManifestError(
                f"{label} role {roles[0]!r} is not accepted; "
                f"the 'role' field is reserved for future use"
            )

        pipelock = (
            PipelockRoutePolicy.from_dict(bottle_name, idx, d["pipelock"])
            if "pipelock" in d
            else PipelockRoutePolicy()
        )

        for k in d:
            if k not in ("host", "path_allowlist", "auth", "role", "pipelock"):
                raise ManifestError(
                    f"{label} has unknown key {k!r}; accepted keys are "
                    f"'host', 'path_allowlist', 'auth', 'role', 'pipelock'"
                )

        return cls(
            Host=host,
            PathAllowlist=prefixes,
            AuthScheme=auth_scheme,
            TokenRef=token_ref,
            Role=roles,
            Pipelock=pipelock,
        )


@dataclass(frozen=True)
class EgressConfig:
    """Per-bottle egress configuration. Today this is just the
    route table; the nesting under `egress:` leaves room for
    per-bottle proxy settings (port override, log level, etc.) in
    follow-ups."""

    routes: tuple[EgressRoute, ...] = ()

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "EgressConfig":
        d = _as_json_object(raw, f"bottle '{bottle_name}' egress")
        routes_raw = d.get("routes")
        routes: tuple[EgressRoute, ...] = ()
        if routes_raw is not None:
            if not isinstance(routes_raw, list):
                raise ManifestError(
                    f"bottle '{bottle_name}' egress.routes must be an array "
                    f"(was {type(routes_raw).__name__})"
                )
            routes_list = cast(list[object], routes_raw)
            routes = tuple(
                EgressRoute.from_dict(bottle_name, i, entry)
                for i, entry in enumerate(routes_list)
            )
            _validate_egress_routes(bottle_name, routes)
        for k in d:
            if k != "routes":
                raise ManifestError(
                    f"bottle '{bottle_name}' egress has unknown key {k!r}; "
                    f"only 'routes' is accepted"
                )
        return cls(routes=routes)


@dataclass(frozen=True)
class Bottle:
    env: Mapping[str, str] = field(default_factory=_empty_str_dict)
    agent_provider: AgentProvider = field(default_factory=AgentProvider)
    git: tuple[GitEntry, ...] = ()
    # Per-bottle git identity (issue #86). Empty default — bottles
    # that don't set `git.user:` in the manifest skip the
    # `git config --global` step entirely. Set independently of
    # the `git.remotes:` upstream map above: a bottle can declare a user
    # identity without any git-gate upstreams, and vice versa.
    git_user: GitUser = field(default_factory=GitUser)
    egress: EgressConfig = field(default_factory=EgressConfig)
    # Opt-in per-bottle stuck-recovery sidecar (PRD 0013). When true,
    # the launch step brings up a supervise sidecar that exposes three
    # MCP tools to the agent (cred-proxy-block, pipelock-block,
    # capability-block; the cred-proxy-block tool is renamed and
    # retargeted at egress in PRD 0017 chunk 3) plus mounts the
    # current-config dir read-only into the agent at /etc/bot-bottle/
    # current-config. False (the default) skips the sidecar and mount.
    supervise: bool = False

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "Bottle":
        d = _as_json_object(raw, f"bottle '{name}'")

        if "runtime" in d:
            raise ManifestError(
                f"bottle '{name}' has a 'runtime' field, which is no longer "
                f"supported. gVisor (runsc) is now auto-detected by the "
                f"backend; remove the 'runtime' field from the bottle "
                f"definition."
            )

        if "ssh" in d:
            raise ManifestError(
                f"bottle '{name}' has an 'ssh' field, which has been removed "
                f"(PRD 0009). Move each entry to 'git': declare the upstream "
                f"as a git remote with Name + Upstream URL + IdentityFile, "
                f"and the per-bottle git-gate (PRD 0008) will hold the "
                f"credential and gitleaks-scan pushes."
            )

        if "git_user" in d:
            raise ManifestError(
                f"bottle '{name}' has a 'git_user' field, which has been "
                f"removed. Move it under 'git.user'."
            )

        unknown = set(d.keys()) - _BOTTLE_KEYS
        if unknown:
            allowed = ", ".join(sorted(_BOTTLE_KEYS))
            raise ManifestError(
                f"bottle '{name}' has unknown key(s) {sorted(unknown)}; "
                f"allowed keys are {allowed}."
            )

        env: dict[str, str] = {}
        env_raw = d.get("env")
        if env_raw is not None:
            env_dict = _as_json_object(env_raw, f"bottle '{name}' env")
            for var, value in env_dict.items():
                if not isinstance(value, str):
                    raise ManifestError(
                        f"env entry {var} in bottle '{name}' must be a JSON string "
                        f"(was {type(value).__name__}). Use \"?<message>\" for prompt-at-runtime."
                    )
                env[var] = value

        git: tuple[GitEntry, ...] = ()
        git_user = GitUser()
        git_raw = d.get("git")
        if git_raw is not None:
            git, git_user = _parse_git_config(name, git_raw)

        agent_provider = (
            AgentProvider.from_dict(name, d["agent_provider"])
            if "agent_provider" in d
            else AgentProvider()
        )

        egress = (
            EgressConfig.from_dict(name, d["egress"])
            if "egress" in d
            else EgressConfig()
        )

        supervise_raw = d.get("supervise", False)
        if not isinstance(supervise_raw, bool):
            raise ManifestError(
                f"bottle '{name}' supervise must be a boolean "
                f"(was {type(supervise_raw).__name__})"
            )

        return cls(
            env=env, agent_provider=agent_provider, git=git,
            git_user=git_user, egress=egress, supervise=supervise_raw,
        )


@dataclass(frozen=True)
class Agent:
    bottle: str
    skills: tuple[str, ...] = ()
    prompt: str = ""
    # Per-agent git identity (issue #94). Overlays the referenced
    # bottle's git.user per-field at `Manifest.bottle_for`. Only the
    # `user` block is allowed at the agent level; `git.remotes` stays
    # bottle-only because it carries credentials and host trust.
    git_user: GitUser = GitUser()

    @classmethod
    def from_dict(cls, name: str, raw: object, bottle_names: set[str]) -> "Agent":
        d = _as_json_object(raw, f"agent '{name}'")

        bottle = d.get("bottle")
        if not isinstance(bottle, str) or not bottle:
            raise ManifestError(f"agent '{name}' must declare a 'bottle' field naming a defined bottle")
        if bottle not in bottle_names:
            available = ", ".join(sorted(bottle_names)) or "(none defined)"
            raise ManifestError(
                f"agent '{name}' references bottle '{bottle}', which is not defined. "
                f"Available: {available}"
            )

        skills: tuple[str, ...] = ()
        skills_raw = d.get("skills")
        if skills_raw is not None:
            if not isinstance(skills_raw, list):
                raise ManifestError(f"agent '{name}' skills must be an array (was {type(skills_raw).__name__})")
            collected: list[str] = []
            skills_list = cast(list[object], skills_raw)
            for i, skill in enumerate(skills_list):
                if not isinstance(skill, str):
                    raise ManifestError(
                        f"agent '{name}' skills[{i}] must be a string "
                        f"(was {type(skill).__name__})"
                    )
                collected.append(skill)
            skills = tuple(collected)

        prompt_raw = d.get("prompt")
        if prompt_raw is None:
            prompt = ""
        elif isinstance(prompt_raw, str):
            prompt = prompt_raw
        else:
            raise ManifestError(f"agent '{name}' prompt must be a string (was {type(prompt_raw).__name__})")

        # git: agents may declare only `git.user` (name/email). Any
        # other git key — notably `remotes` — is rejected: remotes
        # carry credentials and host trust and stay bottle-only.
        git_user = GitUser()
        git_raw = d.get("git")
        if git_raw is not None:
            gd = _as_json_object(git_raw, f"agent '{name}' git")
            for k in gd.keys():
                if k != "user":
                    raise ManifestError(
                        f"agent '{name}' git.{k} is not allowed at the "
                        f"agent level; only git.user (name/email) may be "
                        f"set on an agent. git.remotes is bottle-only "
                        f"(it carries credentials and host trust)."
                    )
            if "user" in gd:
                git_user = GitUser.from_dict(name, gd["user"])

        return cls(bottle=bottle, skills=skills, prompt=prompt, git_user=git_user)


@dataclass(frozen=True)
class Manifest:
    bottles: Mapping[str, Bottle]
    agents: Mapping[str, Agent]

    @classmethod
    def resolve(cls, cwd: str, *, missing_ok: bool = False) -> "Manifest":
        """Walk the per-file manifest tree and build a Manifest.

        Layout (PRD 0011):
          $HOME/.bot-bottle/bottles/<name>.md  — bottles (home-only)
          $HOME/.bot-bottle/agents/<name>.md   — home agents
          $CWD/.bot-bottle/agents/<name>.md    — cwd agents

        Cwd agents merge into the home agents on the same name
        (cwd wins). A bottles/ subdir under $CWD is logged as a
        warning and ignored — the filesystem layout IS the trust
        boundary.

        If `missing_ok` is true, a missing `$HOME/.bot-bottle/`
        returns an empty manifest instead of dying. This is for
        passive UI surfaces like the dashboard, which can still
        monitor already-running agents without launch config.

        If `bot-bottle.json` exists alongside a missing
        `.bot-bottle/` directory at either side, dies with a
        clear pointer at the README's manifest section — the
        manifest format changed in PRD 0011 and we don't silently
        fall back."""
        home_dir = Path(os.environ["HOME"])
        cwd_dir = Path(cwd)
        home_md = home_dir / ".bot-bottle"
        cwd_md = cwd_dir / ".bot-bottle"

        _check_stale_json(home_dir, home_md, "$HOME")
        if cwd_dir.resolve() != home_dir.resolve():
            _check_stale_json(cwd_dir, cwd_md, "$CWD")

        if not home_md.is_dir():
            if missing_ok:
                return cls.from_json_obj({"bottles": {}, "agents": {}})
            raise ManifestError(
                f"no manifest found: {home_md} does not exist. "
                f"See README.md for the per-file Markdown layout "
                f"(PRD 0011)."
            )

        # When CWD == HOME (running from $HOME directly), pass the
        # same dir for both — _load_md_dirs will dedupe.
        cwd_md_arg = cwd_md if cwd_md.is_dir() and cwd_dir.resolve() != home_dir.resolve() else None
        return cls.from_md_dirs(home_md, cwd_md_arg)

    @classmethod
    def from_md_dirs(
        cls,
        home_dir: Path,
        cwd_dir: Path | None,
    ) -> "Manifest":
        """Programmatic entry point. Loads bottles from
        `<home_dir>/bottles/`, home agents from `<home_dir>/agents/`,
        and (if `cwd_dir` is passed) cwd agents from
        `<cwd_dir>/agents/`. Cwd agents override home agents on
        name collision. A `bottles/` subdir under `cwd_dir` is
        logged as a warning and ignored.

        Used by tests to build a Manifest from fixture directories
        without touching `os.environ`."""
        bottles_dir = home_dir / "bottles"
        bottles = _load_bottles_from_dir(bottles_dir)

        bottle_names = set(bottles.keys())
        agents_dir = home_dir / "agents"
        agents = _load_agents_from_dir(agents_dir, bottle_names, source="$HOME")

        if cwd_dir is not None:
            stale_bottles = cwd_dir / "bottles"
            if stale_bottles.is_dir():
                files = sorted(stale_bottles.glob("*.md"))
                if files:
                    names = ", ".join(p.name for p in files)
                    warn(
                        f"ignoring bottle file(s) under "
                        f"{stale_bottles}: {names}. Bottles can only "
                        f"live under $HOME/.bot-bottle/bottles/ "
                        f"(PRD 0011). Move them or delete."
                    )
            cwd_agents_dir = cwd_dir / "agents"
            cwd_agents = _load_agents_from_dir(
                cwd_agents_dir, bottle_names, source="$CWD"
            )
            agents = {**agents, **cwd_agents}

        return cls(bottles=bottles, agents=agents)

    @classmethod
    def from_json_obj(cls, obj: object) -> "Manifest":
        """Validate and build a Manifest from a raw JSON-like dict."""
        d = _as_json_object(obj, "manifest")
        raw_bottles_obj = _section_dict(d.get("bottles"), "manifest 'bottles'")
        raw_agents = _section_dict(d.get("agents"), "manifest 'agents'")

        # Coerce each bottle's raw to dict[str, object] so the
        # PRD 0025 resolver can apply extends-merge rules
        # consistently with the md-loader path.
        raw_bottles: dict[str, dict[str, object]] = {}
        for n, b in raw_bottles_obj.items():
            raw_bottles[n] = _as_json_object(b, f"bottle '{n}'")
        bottles = _resolve_bottles(raw_bottles)

        bottle_names = set(bottles.keys())
        agents: dict[str, Agent] = {
            n: Agent.from_dict(n, a, bottle_names) for n, a in raw_agents.items()
        }
        return cls(bottles=bottles, agents=agents)

    def has_agent(self, name: str) -> bool:
        return name in self.agents

    def require_agent(self, name: str) -> None:
        if self.has_agent(name):
            return
        available = ", ".join(self.agents.keys())
        if available:
            raise ManifestError(f"agent '{name}' not defined in bot-bottle.json. Available: {available}")
        raise ManifestError(f"agent '{name}' not defined in bot-bottle.json (manifest is empty).")

    def has_bottle(self, name: str) -> bool:
        return name in self.bottles

    def require_bottle(self, name: str) -> None:
        if self.has_bottle(name):
            return
        available = ", ".join(self.bottles.keys())
        if available:
            raise ManifestError(
                f"bottle '{name}' not defined in bot-bottle.json. "
                f"Available bottles: {available}"
            )
        raise ManifestError(f"bottle '{name}' not defined in bot-bottle.json (no bottles defined).")

    def _effective_git_user(self, agent_name: str) -> GitUser:
        """Merge the agent's git.user over the referenced bottle's,
        per-field, agent-wins-on-non-empty (issue #94). Same overlay
        the `extends:` resolver applies between bottles
        (`_merge_bottles`)."""
        agent = self.agents[agent_name]
        base = self.bottles[agent.bottle].git_user
        over = agent.git_user
        if over.is_empty():
            return base
        return GitUser(
            name=over.name or base.name,
            email=over.email or base.email,
        )

    def bottle_for(self, agent_name: str) -> Bottle:
        """Resolve the Bottle the named agent references, with the
        agent's git.user overlaid on top. The validator guarantees both
        lookups succeed for a manifest built via from_json_obj.

        The overlay lives here, the single point both backends call to
        resolve an agent's bottle, so the docker / smolmachines git
        provisioners pick up the merged identity unchanged."""
        bottle = self.bottles[self.agents[agent_name].bottle]
        merged = self._effective_git_user(agent_name)
        if merged == bottle.git_user:
            return bottle
        return replace(bottle, git_user=merged)

    def git_identity_summary(self, agent_name: str) -> str | None:
        """One-line effective git identity with per-field provenance
        for launch summaries, e.g.
        `name=claude (agent), email=eric@dideric.is (bottle)`.
        Returns None when neither agent nor bottle sets an identity."""
        over = self.agents[agent_name].git_user
        merged = self._effective_git_user(agent_name)
        if merged.is_empty():
            return None
        parts: list[str] = []
        if merged.name:
            parts.append(f"name={merged.name} ({'agent' if over.name else 'bottle'})")
        if merged.email:
            parts.append(f"email={merged.email} ({'agent' if over.email else 'bottle'})")
        return ", ".join(parts)


def _as_json_object(value: object, label: str) -> dict[str, object]:
    """Assert that `value` is a JSON object (str-keyed dict) and return
    a view typed as `dict[str, object]` so downstream `.get(...)` calls
    have a typed surface."""
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a JSON object (was {type(value).__name__})")
    items = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for k, v in items.items():
        if not isinstance(k, str):
            raise ManifestError(f"{label} keys must be strings (found {type(k).__name__})")
        out[k] = v
    return out


def _section_dict(value: object, label: str) -> dict[str, object]:
    """Like _as_json_object but treats absent/null as an empty section."""
    if value is None:
        return {}
    return _as_json_object(value, label)


def _opt_str(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be a string (was {type(value).__name__})")
    return value


def _opt_extra_hosts(value: object, label: str) -> dict[str, str]:
    """Validate a `{hostname: ip}` object and return a plain dict. None
    yields an empty dict so callers can treat ExtraHosts as always
    present. IP format is not checked here; docker validates at
    `--add-host` time."""
    if value is None:
        return {}
    obj = _as_json_object(value, label)
    out: dict[str, str] = {}
    for host, ip in obj.items():
        if not host:
            raise ManifestError(f"{label} contains an empty hostname key")
        if not isinstance(ip, str):
            raise ManifestError(f"{label}['{host}'] must be a string (was {type(ip).__name__})")
        if not ip:
            raise ManifestError(f"{label}['{host}'] must be a non-empty string")
        out[host] = ip
    return out


def _parse_git_upstream(url: str, label: str) -> tuple[str, str, str, str]:
    """Parse `ssh://user@host[:port]/path` into (user, host, port, path).
    Dies if `url` doesn't match the ssh:// shape v1 supports. Default
    port is 22 (matches OpenSSH)."""
    if not url.startswith("ssh://"):
        raise ManifestError(f"{label} must be an ssh:// URL (was {url!r})")
    rest = url[len("ssh://"):]
    if "@" not in rest:
        raise ManifestError(f"{label} must include a user (e.g. ssh://git@host/path.git); was {url!r}")
    user, _, hostpart = rest.partition("@")
    if not user:
        raise ManifestError(f"{label} user is empty in {url!r}")
    if "/" not in hostpart:
        raise ManifestError(f"{label} must include a path (e.g. ssh://git@host/path.git); was {url!r}")
    hostport, _, path = hostpart.partition("/")
    if not path:
        raise ManifestError(f"{label} path is empty in {url!r}")
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        if not port.isdigit():
            raise ManifestError(f"{label} port must be numeric in {url!r}")
    else:
        host = hostport
        port = "22"
    if not host:
        raise ManifestError(f"{label} host is empty in {url!r}")
    return (user, host, port, path)


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _validate_egress_routes(
    bottle_name: str,
    routes: tuple[EgressRoute, ...],
) -> None:
    """Cross-validation for `bottle.egress.routes`: hosts must be unique.

    The proxy matches by exact-host (v1); duplicate hosts leave the
    route choice ambiguous so we reject them up front.

    No cross-validation against `bottle.git` is performed. git-gate
    (SSH push/fetch) and egress (HTTPS) broker different protocols;
    declaring both for the same host is a legitimate dev setup."""
    seen_hosts: dict[str, None] = {}
    for r in routes:
        key = r.Host.lower()
        if key in seen_hosts:
            raise ManifestError(
                f"bottle '{bottle_name}' egress.routes has duplicate host "
                f"{r.Host!r}; each host must be unique on the proxy."
            )
        seen_hosts[key] = None


def _validate_unique_git_names(bottle_name: str, git: tuple[GitEntry, ...]) -> None:
    seen: dict[str, None] = {}
    for g in git:
        if g.Name in seen:
            raise ManifestError(
                f"bottle '{bottle_name}' git entries have duplicate Name '{g.Name}'; "
                f"each entry maps to a distinct bare repo on the gate."
            )
        seen[g.Name] = None




# --- Per-file MD loader (PRD 0011) ----------------------------------------

# Filename-as-key uses kebab-case ASCII. The first character is a
# letter so we don't conflict with hidden files / Markdown special
# names (`.md`, `_template.md`, etc.). Filenames that fail this
# pattern are skipped with a warning rather than crashing the load.
_FILENAME_RX = re.compile(r"^[a-z][a-z0-9-]*$")

# Frontmatter keys we accept on each entity. Anything not in these
# sets dies with a "did you mean" pointer — typos shouldn't silently
# ghost into an empty config.
_BOTTLE_KEYS = frozenset(
    {"env", "extends", "agent_provider", "git", "egress", "supervise"}
)
_AGENT_KEYS_REQUIRED = frozenset({"bottle"})
_AGENT_KEYS_OPTIONAL = frozenset({"skills", "git"})
# Claude Code subagent fields bot-bottle ignores at launch but
# doesn't reject — lets the same file double as `~/.claude/agents/*.md`.
_AGENT_KEYS_CC_PASSTHROUGH = frozenset({
    "name", "description", "model", "color", "memory",
})
_AGENT_KEYS = (
    _AGENT_KEYS_REQUIRED | _AGENT_KEYS_OPTIONAL | _AGENT_KEYS_CC_PASSTHROUGH
)


def _check_stale_json(dir_path: Path, md_dir: Path, label: str) -> None:
    """Die if `<dir_path>/bot-bottle.json` exists but `md_dir` does
    not — the manifest format changed in PRD 0011 and we don't want
    to silently leave the JSON content unused."""
    legacy = dir_path / "bot-bottle.json"
    if legacy.is_file() and not md_dir.exists():
        raise ManifestError(
            f"found {legacy} but {md_dir} does not exist. The manifest "
            f"format changed in PRD 0011 — rewrite the JSON content "
            f"as per-file Markdown under {md_dir}/bottles/ and "
            f"{md_dir}/agents/. See README.md for the schema. "
            f"({label})"
        )


def _entity_name_from_path(path: Path) -> str | None:
    """Return the entity name implied by the filename, or None if
    the filename doesn't fit the [a-z][a-z0-9-]* convention. None
    triggers a skip-with-warning at the caller."""
    if path.suffix != ".md":
        return None
    stem = path.stem
    if not _FILENAME_RX.match(stem):
        return None
    return stem


def _load_bottles_from_dir(bottles_dir: Path) -> dict[str, Bottle]:
    """Walk `<bottles_dir>/*.md`, parse each as a bottle, return
    `{name: Bottle}`. Missing dir → empty dict (the user simply
    hasn't declared any bottles yet).

    Two-pass to resolve PRD 0025 `extends:` chains:
      1. Collect each file's raw frontmatter into `{name: raw}`.
      2. Recursively merge `extends:` chains into effective
         Bottle objects (`_resolve_bottles`)."""
    raws: dict[str, dict[str, object]] = {}
    if not bottles_dir.is_dir():
        return {}
    for path in sorted(bottles_dir.glob("*.md")):
        name = _entity_name_from_path(path)
        if name is None:
            warn(
                f"skipping {path}: filename must match "
                f"[a-z][a-z0-9-]*.md (got {path.name!r})"
            )
            continue
        try:
            fm, _body = parse_frontmatter(path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {path}: {e}")
        except YamlSubsetError as e:
            raise ManifestError(f"{path}: {e}")
        unknown = set(fm.keys()) - _BOTTLE_KEYS
        if unknown:
            allowed = ", ".join(sorted(_BOTTLE_KEYS))
            raise ManifestError(
                f"bottle file {path}: unknown frontmatter key(s) "
                f"{sorted(unknown)}; allowed keys are {allowed}."
            )
        raws[name] = fm
    return _resolve_bottles(raws)


def _resolve_bottles(raws: dict[str, dict[str, object]]) -> dict[str, Bottle]:
    """Apply `extends:` chains (PRD 0025) and return a flat
    `{name: Bottle}` of resolved configs. Cycle / missing-parent
    / self-reference die with a clear pointer."""
    cache: dict[str, Bottle] = {}
    for name in raws:
        if name not in cache:
            _resolve_one_bottle(name, raws, cache, ())
    return cache


def _resolve_one_bottle(
    name: str,
    raws: dict[str, dict[str, object]],
    cache: dict[str, Bottle],
    seen: tuple[str, ...],
) -> Bottle:
    """Recursive resolver. `seen` is the current extends-chain for
    cycle detection; on cycle die with the chain so the operator
    can see which two files to break the loop in."""
    if name in cache:
        return cache[name]
    if name in seen:
        chain = " -> ".join(seen + (name,))
        raise ManifestError(f"bottle '{name}' is in an extends cycle: {chain}")
    raw = raws[name]
    parent_name_raw = raw.get("extends")
    # Strip `extends:` before passing to Bottle.from_dict so it
    # isn't accidentally treated as a real Bottle field by future
    # schema additions. It's only meaningful here.
    child_raw = {k: v for k, v in raw.items() if k != "extends"}

    if parent_name_raw is None:
        bottle = Bottle.from_dict(name, child_raw)
        cache[name] = bottle
        return bottle

    if not isinstance(parent_name_raw, str):
        raise ManifestError(
            f"bottle '{name}' extends must be a string "
            f"(was {type(parent_name_raw).__name__})"
        )
    parent_name: str = parent_name_raw
    if parent_name == name:
        raise ManifestError(
            f"bottle '{name}' extends itself; remove the "
            f"self-reference"
        )
    if parent_name not in raws:
        avail = ", ".join(sorted(raws.keys())) or "(none)"
        raise ManifestError(
            f"bottle '{name}' extends '{parent_name}' which is not "
            f"defined. Available bottles: {avail}"
        )
    parent = _resolve_one_bottle(parent_name, raws, cache, seen + (name,))
    bottle = _merge_bottles(parent, child_raw, name)
    cache[name] = bottle
    return bottle


def _merge_bottles(
    parent: Bottle,
    child_raw: dict[str, object],
    name: str,
) -> Bottle:
    """Apply PRD 0025 merge rules: parent is base; child's declared
    fields overlay. env merges dict-style with child-wins on key
    collision; git.user overlays per-field; git.remotes merges by
    upstream host with child entries replacing duplicate hosts."""
    # Parse the child's declared fields into a Bottle (with the
    # usual defaults for anything missing). Validation runs the same
    # way it would for a leaf bottle — typos / wrong types die here.
    child = Bottle.from_dict(name, child_raw)

    # env: dict merge, child wins on collision.
    merged_env = {**parent.env, **child.env}

    # git.user: per-field overlay. Each non-empty field on child
    # wins; empties fall through to parent. The default GitUser()
    # is two empty strings, so a child that omits git.user
    # inherits the parent's user verbatim.
    merged_git_user = GitUser(
        name=child.git_user.name or parent.git_user.name,
        email=child.git_user.email or parent.git_user.email,
    )

    # git.remotes: missing means inherit; an explicit empty object
    # clears; otherwise parent and child merge by UpstreamHost with
    # child entries replacing duplicate hosts.
    if _child_declares_git_remotes(child_raw):
        merged_git = _merge_git_remotes(parent.git, child.git) if child.git else ()
    else:
        merged_git = parent.git

    # Presence-driven full-replace for the remaining list-valued +
    # scalar fields.
    merged_egress = child.egress if "egress" in child_raw else parent.egress
    merged_agent_provider = (
        child.agent_provider
        if "agent_provider" in child_raw
        else parent.agent_provider
    )
    merged_supervise = (
        child.supervise if "supervise" in child_raw else parent.supervise
    )
    _validate_egress_routes(name, merged_egress.routes)

    return Bottle(
        env=merged_env,
        agent_provider=merged_agent_provider,
        git=merged_git,
        git_user=merged_git_user,
        egress=merged_egress,
        supervise=merged_supervise,
    )


def _child_declares_git_remotes(child_raw: dict[str, object]) -> bool:
    git_raw = child_raw.get("git")
    if git_raw is None:
        return False
    git_obj = _as_json_object(git_raw, "child git")
    return "remotes" in git_obj


def _merge_git_remotes(
    parent: tuple[GitEntry, ...],
    child: tuple[GitEntry, ...],
) -> tuple[GitEntry, ...]:
    by_host = {entry.UpstreamHost: entry for entry in parent}
    for entry in child:
        by_host[entry.UpstreamHost] = entry
    return tuple(by_host.values())


def _load_agents_from_dir(
    agents_dir: Path,
    bottle_names: set[str],
    *,
    source: str,
) -> dict[str, Agent]:
    """Walk `<agents_dir>/*.md`, parse each as an agent, return
    `{name: Agent}`. The Markdown body becomes the agent's
    `prompt`. Missing dir → empty dict."""
    out: dict[str, Agent] = {}
    if not agents_dir.is_dir():
        return out
    for path in sorted(agents_dir.glob("*.md")):
        name = _entity_name_from_path(path)
        if name is None:
            warn(
                f"skipping {path}: filename must match "
                f"[a-z][a-z0-9-]*.md (got {path.name!r})"
            )
            continue
        try:
            fm, body = parse_frontmatter(path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {path}: {e}")
        except YamlSubsetError as e:
            raise ManifestError(f"{path}: {e}")
        unknown = set(fm.keys()) - _AGENT_KEYS
        if unknown:
            allowed = ", ".join(sorted(_AGENT_KEYS))
            raise ManifestError(
                f"agent file {path}: unknown frontmatter key(s) "
                f"{sorted(unknown)}; allowed keys are {allowed}."
            )
        # Build the dict Agent.from_dict expects. The body becomes
        # prompt; CC passthrough fields stay in fm and get ignored
        # by from_dict (which reads bottle/skills/git/prompt).
        agent_dict: dict[str, object] = {
            "bottle": fm.get("bottle"),
            "skills": fm.get("skills", []),
            "prompt": body.strip(),
        }
        if "git" in fm:
            agent_dict["git"] = fm["git"]
        out[name] = Agent.from_dict(name, agent_dict, bottle_names)
    return out
