"""Manifest dataclasses. Read claude-bottle.json (cwd + $HOME, deep-merged)
into a frozen, validated Manifest tree.

Schema (see CLAUDE.md "Intended design"):
  {
    "bottles": {
      "<bottle-name>": {
        "env":        { "<NAME>": <env-entry>, ... },
        "git":        [ <git-entry>, ... ],
        "cred_proxy": { "routes": [ <route>, ... ] },
        "egress":     { "allowlist": [ "<hostname>", ... ] }
      }
    },
    "agents": {
      "<agent-name>": {
        "skills":  [ "<skill-name>", ... ],
        "prompt":  "<string>",
        "bottle":  "<bottle-name>"
      }
    }
  }

Bottles group shared infrastructure (git upstreams + their gate credentials,
egress allowlist) that multiple agents can reference. Every agent must
reference a bottle.

Validation runs once at construction (Manifest.from_json_obj) so getters
can trust the shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, cast

from .log import die


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
    UpstreamUser: str = ""
    UpstreamHost: str = ""
    UpstreamPort: str = ""
    UpstreamPath: str = ""

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "GitEntry":
        d = _as_json_object(raw, f"bottle '{bottle_name}' git[{idx}]")
        name = d.get("Name")
        if not isinstance(name, str) or not name:
            die(f"bottle '{bottle_name}' git[{idx}] missing required string field 'Name'")
        upstream = d.get("Upstream")
        if not isinstance(upstream, str) or not upstream:
            die(
                f"bottle '{bottle_name}' git '{name}' missing required string field "
                f"'Upstream'"
            )
        ident = d.get("IdentityFile")
        if not isinstance(ident, str) or not ident:
            die(
                f"bottle '{bottle_name}' git '{name}' missing required string field "
                f"'IdentityFile'"
            )
        khk = _opt_str(
            d.get("KnownHostKey"),
            f"bottle '{bottle_name}' git '{name}' KnownHostKey",
        )
        extra_hosts = _opt_extra_hosts(
            d.get("ExtraHosts"), f"bottle '{bottle_name}' git '{name}' ExtraHosts"
        )
        user, host, port, path = _parse_git_upstream(
            upstream, f"bottle '{bottle_name}' git '{name}' Upstream"
        )
        return cls(
            Name=name,
            Upstream=upstream,
            IdentityFile=ident,
            KnownHostKey=khk,
            ExtraHosts=extra_hosts,
            UpstreamUser=user,
            UpstreamHost=host,
            UpstreamPort=port,
            UpstreamPath=path,
        )


CRED_PROXY_AUTH_SCHEMES = ("Bearer", "token")

# Provisioner role tags a route may carry. Each tag drives one
# agent-side rewrite when the cred-proxy sidecar comes up.
#   anthropic-base-url: set ANTHROPIC_BASE_URL=<proxy><path>
#   npm-registry:       write ~/.npmrc registry= <proxy><path>
#   git-insteadof:      write ~/.gitconfig [url "<proxy><path>"]
#                       insteadOf = <route.upstream>/
#   tea-login:          add an entry to ~/.config/tea/config.yml
#                       (login url = <proxy><path>)
# Routes without a `role` are pure proxy entries with no agent-side
# rewrite — useful for upstreams whose tools the user wires up by
# hand.
CRED_PROXY_ROLES = frozenset({
    "anthropic-base-url",
    "npm-registry",
    "git-insteadof",
    "tea-login",
})

# Roles whose semantics imply a single route can carry them. A second
# route claiming the same role would make the provisioner's choice
# ambiguous (which path goes into ANTHROPIC_BASE_URL?).
CRED_PROXY_SINGLETON_ROLES = frozenset({
    "anthropic-base-url",
    "npm-registry",
})


@dataclass(frozen=True)
class CredProxyRoute:
    """One route on the per-bottle cred-proxy sidecar (PRD 0010).

    The agent dials `http://cred-proxy:<port><Path>...`; the sidecar
    strips any inbound `Authorization` header, injects
    `<AuthScheme> <token>` using the value of the host env var named
    by `TokenRef`, and forwards the rest of the request to `Upstream`.

    `Path` is the agent-facing prefix (must start and end with `/`).
    `Upstream` is the upstream base URL (https only) — the request
    path after `Path` is appended to it. `AuthScheme` is the literal
    word that precedes the token in the injected header (`Bearer` for
    most upstreams, `token` for Gitea — sidesteps go-gitea/gitea#16734).
    `TokenRef` names the host env var holding the credential value;
    the CLI reads it at launch and forwards into the sidecar's environ.
    `Role` carries optional provisioner tags (see CRED_PROXY_ROLES).

    `UpstreamHost` is parsed from `Upstream` for the pipelock allowlist
    + the git-insteadof suppression check."""

    Path: str
    Upstream: str
    AuthScheme: str
    TokenRef: str
    Role: tuple[str, ...] = ()
    UpstreamHost: str = ""

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "CredProxyRoute":
        label = f"bottle '{bottle_name}' cred_proxy.routes[{idx}]"
        d = _as_json_object(raw, label)
        path = d.get("path")
        if not isinstance(path, str) or not path:
            die(f"{label} missing required string field 'path'")
        if not (path.startswith("/") and path.endswith("/")):
            die(f"{label} path {path!r} must start and end with '/'")
        upstream = d.get("upstream")
        if not isinstance(upstream, str) or not upstream:
            die(f"{label} missing required string field 'upstream'")
        host = _parse_https_host(upstream, f"{label} upstream")
        auth_scheme = d.get("auth_scheme")
        if not isinstance(auth_scheme, str) or not auth_scheme:
            die(f"{label} missing required string field 'auth_scheme'")
        if auth_scheme not in CRED_PROXY_AUTH_SCHEMES:
            die(
                f"{label} auth_scheme {auth_scheme!r} is not one of "
                f"{', '.join(CRED_PROXY_AUTH_SCHEMES)}"
            )
        token_ref = d.get("token_ref")
        if not isinstance(token_ref, str) or not token_ref:
            die(
                f"{label} missing required string field 'token_ref' "
                f"(name of the host env var holding the token value)"
            )
        role_raw = d.get("role")
        roles: tuple[str, ...] = ()
        if role_raw is None:
            roles = ()
        elif isinstance(role_raw, str):
            roles = (role_raw,)
        elif isinstance(role_raw, list):
            role_list = cast(list[object], role_raw)
            collected: list[str] = []
            for r in role_list:
                if not isinstance(r, str):
                    die(f"{label} role items must be strings (got {type(r).__name__})")
                collected.append(r)
            roles = tuple(collected)
        else:
            die(
                f"{label} role must be a string or a list of strings "
                f"(was {type(role_raw).__name__})"
            )
        for r in roles:
            if r not in CRED_PROXY_ROLES:
                die(
                    f"{label} role {r!r} is not one of "
                    f"{', '.join(sorted(CRED_PROXY_ROLES))}"
                )
        return cls(
            Path=path,
            Upstream=upstream,
            AuthScheme=auth_scheme,
            TokenRef=token_ref,
            Role=roles,
            UpstreamHost=host,
        )


@dataclass(frozen=True)
class CredProxyConfig:
    """Per-bottle cred-proxy configuration. Today this is just the
    route table; the nesting under `cred_proxy:` leaves room for
    per-bottle proxy settings (port override, log level, etc.) in
    follow-ups."""

    routes: tuple[CredProxyRoute, ...] = ()

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "CredProxyConfig":
        d = _as_json_object(raw, f"bottle '{bottle_name}' cred_proxy")
        routes_raw = d.get("routes")
        routes: tuple[CredProxyRoute, ...] = ()
        if routes_raw is not None:
            if not isinstance(routes_raw, list):
                die(
                    f"bottle '{bottle_name}' cred_proxy.routes must be an array "
                    f"(was {type(routes_raw).__name__})"
                )
            routes_list = cast(list[object], routes_raw)
            routes = tuple(
                CredProxyRoute.from_dict(bottle_name, i, entry)
                for i, entry in enumerate(routes_list)
            )
            _validate_cred_proxy_routes(bottle_name, routes)
        return cls(routes=routes)


DLP_ACTIONS = ("block", "warn")


@dataclass(frozen=True)
class BottleEgress:
    allowlist: tuple[str, ...] = ()
    # Action pipelock takes when its DLP layer matches a credential
    # pattern in a request body. "block" → 403 from the proxy, the
    # request never leaves the egress network. "warn" → forward the
    # request and emit a log line. Default is "block": detect-only
    # would let real secrets escape under the agent's compromised
    # tooling, which is the threat model claude-bottle was built for.
    dlp_action: str = "block"

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "BottleEgress":
        d = _as_json_object(raw, f"bottle '{bottle_name}' egress")
        allow = d.get("allowlist")
        items: list[str] = []
        if allow is not None:
            if not isinstance(allow, list):
                die(
                    f"bottle '{bottle_name}' egress.allowlist must be an array "
                    f"(was {type(allow).__name__})"
                )
            allow_list = cast(list[object], allow)
            for i, host in enumerate(allow_list):
                if not isinstance(host, str):
                    die(
                        f"bottle '{bottle_name}' egress.allowlist[{i}] must be a string "
                        f"(was {type(host).__name__})"
                    )
                items.append(host)
        dlp_action_raw = d.get("dlp_action")
        if dlp_action_raw is None:
            dlp_action = "block"
        elif isinstance(dlp_action_raw, str):
            if dlp_action_raw not in DLP_ACTIONS:
                die(
                    f"bottle '{bottle_name}' egress.dlp_action must be one of "
                    f"{', '.join(DLP_ACTIONS)} (was {dlp_action_raw!r})"
                )
            dlp_action = dlp_action_raw
        else:
            die(
                f"bottle '{bottle_name}' egress.dlp_action must be a string "
                f"(was {type(dlp_action_raw).__name__})"
            )
        return cls(allowlist=tuple(items), dlp_action=dlp_action)


@dataclass(frozen=True)
class Bottle:
    env: Mapping[str, str] = field(default_factory=_empty_str_dict)
    git: tuple[GitEntry, ...] = ()
    cred_proxy: CredProxyConfig = field(default_factory=CredProxyConfig)
    egress: BottleEgress = field(default_factory=BottleEgress)

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "Bottle":
        d = _as_json_object(raw, f"bottle '{name}'")

        if "runtime" in d:
            die(
                f"bottle '{name}' has a 'runtime' field, which is no longer "
                f"supported. gVisor (runsc) is now auto-detected by the "
                f"backend; remove the 'runtime' field from the bottle "
                f"definition."
            )

        if "ssh" in d:
            die(
                f"bottle '{name}' has an 'ssh' field, which has been removed "
                f"(PRD 0009). Move each entry to 'git': declare the upstream "
                f"as a git remote with Name + Upstream URL + IdentityFile, "
                f"and the per-bottle git-gate (PRD 0008) will hold the "
                f"credential and gitleaks-scan pushes."
            )

        env: dict[str, str] = {}
        env_raw = d.get("env")
        if env_raw is not None:
            env_dict = _as_json_object(env_raw, f"bottle '{name}' env")
            for var, value in env_dict.items():
                if not isinstance(value, str):
                    die(
                        f"env entry {var} in bottle '{name}' must be a JSON string "
                        f"(was {type(value).__name__}). Use \"?<message>\" for prompt-at-runtime."
                    )
                env[var] = value

        git: tuple[GitEntry, ...] = ()
        git_raw = d.get("git")
        if git_raw is not None:
            if not isinstance(git_raw, list):
                die(f"bottle '{name}' git must be an array (was {type(git_raw).__name__})")
            git_list = cast(list[object], git_raw)
            git = tuple(
                GitEntry.from_dict(name, i, entry)
                for i, entry in enumerate(git_list)
            )
            _validate_unique_git_names(name, git)

        if "tokens" in d:
            die(
                f"bottle '{name}' has a 'tokens' field. The shape was reworked: "
                f"each route now lives under 'cred_proxy.routes' with explicit "
                f"path / upstream / auth_scheme / token_ref / role[]. See "
                f"docs/prds/0010-cred-proxy.md."
            )

        cred_proxy = (
            CredProxyConfig.from_dict(name, d["cred_proxy"])
            if "cred_proxy" in d
            else CredProxyConfig()
        )

        egress_raw = d.get("egress")
        egress = (
            BottleEgress.from_dict(name, egress_raw)
            if egress_raw is not None
            else BottleEgress()
        )

        return cls(env=env, git=git, cred_proxy=cred_proxy, egress=egress)


@dataclass(frozen=True)
class Agent:
    bottle: str
    skills: tuple[str, ...] = ()
    prompt: str = ""

    @classmethod
    def from_dict(cls, name: str, raw: object, bottle_names: set[str]) -> "Agent":
        d = _as_json_object(raw, f"agent '{name}'")

        bottle = d.get("bottle")
        if not isinstance(bottle, str) or not bottle:
            die(f"agent '{name}' must declare a 'bottle' field naming a defined bottle")
        if bottle not in bottle_names:
            available = ", ".join(sorted(bottle_names)) or "(none defined)"
            die(
                f"agent '{name}' references bottle '{bottle}', which is not defined. "
                f"Available: {available}"
            )

        skills: tuple[str, ...] = ()
        skills_raw = d.get("skills")
        if skills_raw is not None:
            if not isinstance(skills_raw, list):
                die(f"agent '{name}' skills must be an array (was {type(skills_raw).__name__})")
            collected: list[str] = []
            skills_list = cast(list[object], skills_raw)
            for i, skill in enumerate(skills_list):
                if not isinstance(skill, str):
                    die(
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
            die(f"agent '{name}' prompt must be a string (was {type(prompt_raw).__name__})")

        return cls(bottle=bottle, skills=skills, prompt=prompt)


@dataclass(frozen=True)
class Manifest:
    bottles: Mapping[str, Bottle]
    agents: Mapping[str, Agent]

    @classmethod
    def resolve(cls, cwd: str) -> "Manifest":
        """Look for claude-bottle.json in <cwd> and in $HOME, deep-merge
        them (cwd entries override home entries on key conflict for both
        bottles and agents), then validate. Dies if neither file is
        found, either is invalid JSON, or the merged shape violates the
        schema."""
        cwd_file = Path(cwd) / "claude-bottle.json"
        home_file = Path(os.environ["HOME"]) / "claude-bottle.json"

        cwd_doc = _load_json_or_die(cwd_file) if cwd_file.is_file() else None
        home_doc = _load_json_or_die(home_file) if home_file.is_file() else None

        if cwd_doc is None and home_doc is None:
            die(f"no claude-bottle.json found in {cwd} or {os.environ['HOME']}")

        h: dict[str, object] = home_doc if home_doc is not None else {}
        c: dict[str, object] = cwd_doc if cwd_doc is not None else {}
        h_bottles = _section_dict(h.get("bottles"), "bottles")
        c_bottles = _section_dict(c.get("bottles"), "bottles")
        h_agents = _section_dict(h.get("agents"), "agents")
        c_agents = _section_dict(c.get("agents"), "agents")
        merged: dict[str, object] = {
            "bottles": {**h_bottles, **c_bottles},
            "agents": {**h_agents, **c_agents},
        }
        return cls.from_json_obj(merged)

    @classmethod
    def from_json_obj(cls, obj: object) -> "Manifest":
        """Validate and build a Manifest from a raw JSON-like dict."""
        d = _as_json_object(obj, "manifest")
        raw_bottles = _section_dict(d.get("bottles"), "manifest 'bottles'")
        raw_agents = _section_dict(d.get("agents"), "manifest 'agents'")

        bottles: dict[str, Bottle] = {
            n: Bottle.from_dict(n, b) for n, b in raw_bottles.items()
        }
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
            die(f"agent '{name}' not defined in claude-bottle.json. Available: {available}")
        die(f"agent '{name}' not defined in claude-bottle.json (manifest is empty).")

    def has_bottle(self, name: str) -> bool:
        return name in self.bottles

    def require_bottle(self, name: str) -> None:
        if self.has_bottle(name):
            return
        available = ", ".join(self.bottles.keys())
        if available:
            die(
                f"bottle '{name}' not defined in claude-bottle.json. "
                f"Available bottles: {available}"
            )
        die(f"bottle '{name}' not defined in claude-bottle.json (no bottles defined).")

    def bottle_for(self, agent_name: str) -> Bottle:
        """Resolve the Bottle the named agent references. The validator
        guarantees both lookups succeed for a manifest built via
        from_json_obj."""
        return self.bottles[self.agents[agent_name].bottle]


def _as_json_object(value: object, label: str) -> dict[str, object]:
    """Assert that `value` is a JSON object (str-keyed dict) and return
    a view typed as `dict[str, object]` so downstream `.get(...)` calls
    have a typed surface."""
    if not isinstance(value, dict):
        die(f"{label} must be a JSON object (was {type(value).__name__})")
    items = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for k, v in items.items():
        if not isinstance(k, str):
            die(f"{label} keys must be strings (found {type(k).__name__})")
        out[k] = v
    return out


def _section_dict(value: object, label: str) -> dict[str, object]:
    """Like _as_json_object but treats absent/null as an empty section."""
    if value is None:
        return {}
    return _as_json_object(value, label)


def _load_json_or_die(path: Path) -> dict[str, object]:
    try:
        with path.open() as f:
            doc: object = json.load(f)
    except json.JSONDecodeError:
        die(f"claude-bottle.json at {path} is not valid JSON")
    return _as_json_object(doc, f"claude-bottle.json at {path}")


def _opt_str(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        die(f"{label} must be a string (was {type(value).__name__})")
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
            die(f"{label} contains an empty hostname key")
        if not isinstance(ip, str):
            die(f"{label}['{host}'] must be a string (was {type(ip).__name__})")
        if not ip:
            die(f"{label}['{host}'] must be a non-empty string")
        out[host] = ip
    return out


def _parse_git_upstream(url: str, label: str) -> tuple[str, str, str, str]:
    """Parse `ssh://user@host[:port]/path` into (user, host, port, path).
    Dies if `url` doesn't match the ssh:// shape v1 supports. Default
    port is 22 (matches OpenSSH)."""
    if not url.startswith("ssh://"):
        die(f"{label} must be an ssh:// URL (was {url!r})")
    rest = url[len("ssh://"):]
    if "@" not in rest:
        die(f"{label} must include a user (e.g. ssh://git@host/path.git); was {url!r}")
    user, _, hostpart = rest.partition("@")
    if not user:
        die(f"{label} user is empty in {url!r}")
    if "/" not in hostpart:
        die(f"{label} must include a path (e.g. ssh://git@host/path.git); was {url!r}")
    hostport, _, path = hostpart.partition("/")
    if not path:
        die(f"{label} path is empty in {url!r}")
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        if not port.isdigit():
            die(f"{label} port must be numeric in {url!r}")
    else:
        host = hostport
        port = "22"
    if not host:
        die(f"{label} host is empty in {url!r}")
    return (user, host, port, path)


def _parse_https_host(url: str, label: str) -> str:
    """Extract the host from an `https://host[:port][/path]` URL.
    Dies if `url` is not an https:// URL or the host segment is empty.
    Used to derive `CredProxyRoute.UpstreamHost` from a route's
    `upstream` so pipelock's allowlist (and the provisioner's git-gate
    overlap check) can match on host alone."""
    if not url.startswith("https://"):
        die(f"{label} must be an https:// URL (was {url!r})")
    rest = url[len("https://"):]
    hostport, _, _ = rest.partition("/")
    host, _, _port = hostport.partition(":")
    if not host:
        die(f"{label} host is empty in {url!r}")
    return host


def _validate_cred_proxy_routes(
    bottle_name: str,
    routes: tuple[CredProxyRoute, ...],
) -> None:
    """Cross-validation for `bottle.cred_proxy.routes`:

    - Paths must be unique within the bottle (the proxy routes by
      longest-prefix match; duplicate paths leave the choice
      undefined).
    - Singleton roles (`anthropic-base-url`, `npm-registry`) may
      appear on at most one route — the provisioner uses them to
      write a single dotfile entry, so two routes claiming the role
      would make the choice ambiguous.

    No cross-validation against `bottle.git` is performed. git-gate
    (SSH push/fetch) and cred-proxy (HTTPS REST + git smart-HTTP
    fetch) broker different protocols; declaring both on the same
    host is a legitimate dev setup.
    """
    seen_paths: dict[str, None] = {}
    for r in routes:
        if r.Path in seen_paths:
            die(
                f"bottle '{bottle_name}' cred_proxy.routes has duplicate path "
                f"{r.Path!r}; each path must be unique on the proxy."
            )
        seen_paths[r.Path] = None
    for role in CRED_PROXY_SINGLETON_ROLES:
        with_role = [r for r in routes if role in r.Role]
        if len(with_role) > 1:
            paths = ", ".join(r.Path for r in with_role)
            die(
                f"bottle '{bottle_name}' cred_proxy.routes has {len(with_role)} "
                f"routes with role {role!r} (paths: {paths}); this role drives a "
                f"single agent-side rewrite — pick one."
            )


def _validate_unique_git_names(bottle_name: str, git: tuple[GitEntry, ...]) -> None:
    seen: dict[str, None] = {}
    for g in git:
        if g.Name in seen:
            die(
                f"bottle '{bottle_name}' git entries have duplicate Name '{g.Name}'; "
                f"each entry maps to a distinct bare repo on the gate."
            )
        seen[g.Name] = None


