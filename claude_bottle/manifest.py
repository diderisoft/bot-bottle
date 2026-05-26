"""Manifest dataclasses (PRD 0011 layout).

Reads the per-file manifest tree:

  $HOME/.claude-bottle/bottles/<name>.md   — one bottle per file
  $HOME/.claude-bottle/agents/<name>.md    — home-resident agents
  $CWD/.claude-bottle/agents/<name>.md     — cwd-supplied agents

Each file is Markdown with YAML frontmatter. The frontmatter holds
the structured config (see schema below); for agents the body is
the system prompt, for bottles the body is human documentation
(ignored by the parser).

Bottle schema (frontmatter):
  env:          { <NAME>: <env-entry>, ... }
  git:          [ <git-entry>, ... ]
  egress: { routes: [ <egress-route>, ... ] }

Agent schema (frontmatter):
  bottle:        <bottle-name>          # required
  skills:        [ <skill-name>, ... ]   # optional
  # Claude Code subagent passthrough fields — accepted, ignored:
  name, description, model, color, memory

The agent file's Markdown body is the system prompt (stripped).
Unknown top-level frontmatter keys die with a hint.

Bottles can ONLY live under $HOME. A bottles/ dir under $CWD is a
warn at load time and contributes nothing. The trust boundary is
expressed as filesystem layout rather than resolver logic.

Validation runs once at load. Manifest.from_json_obj is preserved
as a programmatic entry point (used by tests) that takes a dict
with the same field names — useful for building manifests without
on-disk files.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, cast

from .log import die, warn
from .yaml_subset import parse_frontmatter


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


# Auth schemes for the egress route's optional `auth` block.
# Same values cred-proxy accepts today; `token` sidesteps the Gitea
# token-not-Bearer quirk (go-gitea/gitea#16734).
EGRESS_AUTH_SCHEMES = ("Bearer", "token")

# Optional per-route role markers. A role signals "this route plays
# a specific named part in the bottle's auth flow"; the launch step
# acts on the marker.
#
#   claude_code_oauth: this route auth-injects on the agent's
#                      claude-code OAuth flow. Triggers prepare.py
#                      to ship a placeholder CLAUDE_CODE_OAUTH_TOKEN
#                      to the agent (so claude-code starts) and
#                      disable nonessential-traffic / error-reporting
#                      env vars. Host doesn't matter to the placeholder
#                      logic — declare the role on whichever route
#                      injects the OAuth header.
#
# Routes without a `role` are pure proxy entries: egress
# enforces path_allowlist + injects auth on its own, but nothing
# special happens on the agent side.
EGRESS_ROLES = frozenset({
    "claude_code_oauth",
})

# Singleton roles may appear on at most one route per bottle.
# claude_code_oauth drives a single placeholder env var; two routes
# claiming it would leave "which one is the canonical OAuth route?"
# ambiguous for any future role-aware logic.
EGRESS_SINGLETON_ROLES = frozenset({
    "claude_code_oauth",
})


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

    `Role` is an optional tuple of named markers (see
    EGRESS_ROLES). The launch step reads these and triggers
    associated side effects (e.g. the `claude_code_oauth` marker
    causes prepare.py to set a placeholder OAuth env on the agent).

    Validation rules (enforced in `from_dict`):
      - `host` required, non-empty.
      - `path_allowlist` optional, list of absolute path prefixes.
      - `auth` optional. If present, MUST carry both `scheme` and
        `token_ref` as non-empty strings; an empty `auth: {}` is an
        error rather than a synonym for "no auth" (omit `auth` for
        that case).
      - `role` optional. String or list of strings drawn from
        EGRESS_ROLES. Singleton roles (see
        EGRESS_SINGLETON_ROLES) may appear on at most one
        route per bottle.
    """

    Host: str
    PathAllowlist: tuple[str, ...] = ()
    AuthScheme: str = ""
    TokenRef: str = ""
    Role: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "EgressRoute":
        label = f"bottle '{bottle_name}' egress.routes[{idx}]"
        d = _as_json_object(raw, label)
        host = d.get("host")
        if not isinstance(host, str) or not host:
            die(f"{label} missing required string field 'host'")

        path_allow_raw = d.get("path_allowlist")
        prefixes: tuple[str, ...] = ()
        if path_allow_raw is not None:
            if not isinstance(path_allow_raw, list):
                die(
                    f"{label} path_allowlist must be an array "
                    f"(was {type(path_allow_raw).__name__})"
                )
            path_list = cast(list[object], path_allow_raw)
            collected: list[str] = []
            for j, p in enumerate(path_list):
                if not isinstance(p, str):
                    die(
                        f"{label} path_allowlist[{j}] must be a string "
                        f"(was {type(p).__name__})"
                    )
                if not p.startswith("/"):
                    die(
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
                die(
                    f"{label} auth is empty ({{}}); omit the 'auth' key "
                    f"entirely if this route is unauthenticated. Otherwise "
                    f"both 'scheme' and 'token_ref' are required."
                )
            auth_scheme_raw = auth_d.get("scheme")
            if not isinstance(auth_scheme_raw, str) or not auth_scheme_raw:
                die(
                    f"{label} auth.scheme is required when 'auth' is set "
                    f"(non-empty string)"
                )
            if auth_scheme_raw not in EGRESS_AUTH_SCHEMES:
                die(
                    f"{label} auth.scheme {auth_scheme_raw!r} is not one of "
                    f"{', '.join(EGRESS_AUTH_SCHEMES)}"
                )
            token_ref_raw = auth_d.get("token_ref")
            if not isinstance(token_ref_raw, str) or not token_ref_raw:
                die(
                    f"{label} auth.token_ref is required when 'auth' is set "
                    f"(name of the host env var holding the token value)"
                )
            for k in auth_d:
                if k not in ("scheme", "token_ref"):
                    die(
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
                    die(f"{label} role items must be strings (got {type(r).__name__})")
                collected_roles.append(r)
            roles = tuple(collected_roles)
        else:
            die(
                f"{label} role must be a string or a list of strings "
                f"(was {type(role_raw).__name__})"
            )
        for r in roles:
            if r not in EGRESS_ROLES:
                die(
                    f"{label} role {r!r} is not one of "
                    f"{', '.join(sorted(EGRESS_ROLES))}"
                )

        for k in d:
            if k not in ("host", "path_allowlist", "auth", "role"):
                die(
                    f"{label} has unknown key {k!r}; accepted keys are "
                    f"'host', 'path_allowlist', 'auth', 'role'"
                )

        return cls(
            Host=host,
            PathAllowlist=prefixes,
            AuthScheme=auth_scheme,
            TokenRef=token_ref,
            Role=roles,
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
                die(
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
                die(
                    f"bottle '{bottle_name}' egress has unknown key {k!r}; "
                    f"only 'routes' is accepted"
                )
        return cls(routes=routes)


@dataclass(frozen=True)
class Bottle:
    env: Mapping[str, str] = field(default_factory=_empty_str_dict)
    git: tuple[GitEntry, ...] = ()
    egress: EgressConfig = field(default_factory=EgressConfig)
    # Opt-in per-bottle stuck-recovery sidecar (PRD 0013). When true,
    # the launch step brings up a supervise sidecar that exposes three
    # MCP tools to the agent (cred-proxy-block, pipelock-block,
    # capability-block; the cred-proxy-block tool is renamed and
    # retargeted at egress in PRD 0017 chunk 3) plus mounts the
    # current-config dir read-only into the agent at /etc/claude-bottle/
    # current-config. False (the default) skips the sidecar and mount.
    supervise: bool = False

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
                f"each route now lives under 'egress.routes' with explicit "
                f"host / path_allowlist / auth. See docs/prds/0017-egress-via-mitmproxy.md."
            )

        if "cred_proxy" in d:
            die(
                f"bottle '{name}' has a 'cred_proxy' field, which has been removed "
                f"(PRD 0017). Rename to 'egress' and migrate each route:\n"
                f"  - 'path' + 'upstream' (cred-proxy URL prefix + upstream URL)\n"
                f"    → 'host' (just the upstream hostname)\n"
                f"  - 'auth_scheme' + 'token_ref' (flat)\n"
                f"    → 'auth: {{ scheme, token_ref }}' (nested, optional)\n"
                f"  - 'role' (provisioner dotfile rewrites): drop — egress "
                f"is on the agent's HTTP_PROXY path, so dotfile rewrites are no "
                f"longer needed.\n"
                f"  - 'path_allowlist' (new): optional URL prefix gate for the "
                f"host.\n"
                f"See docs/prds/0017-egress-via-mitmproxy.md."
            )

        egress = (
            EgressConfig.from_dict(name, d["egress"])
            if "egress" in d
            else EgressConfig()
        )

        supervise_raw = d.get("supervise", False)
        if not isinstance(supervise_raw, bool):
            die(
                f"bottle '{name}' supervise must be a boolean "
                f"(was {type(supervise_raw).__name__})"
            )

        return cls(
            env=env, git=git, egress=egress,
            supervise=supervise_raw,
        )


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
        """Walk the per-file manifest tree and build a Manifest.

        Layout (PRD 0011):
          $HOME/.claude-bottle/bottles/<name>.md  — bottles (home-only)
          $HOME/.claude-bottle/agents/<name>.md   — home agents
          $CWD/.claude-bottle/agents/<name>.md    — cwd agents

        Cwd agents merge into the home agents on the same name
        (cwd wins). A bottles/ subdir under $CWD is logged as a
        warning and ignored — the filesystem layout IS the trust
        boundary.

        If `claude-bottle.json` exists alongside a missing
        `.claude-bottle/` directory at either side, dies with a
        clear pointer at the README's manifest section — the
        manifest format changed in PRD 0011 and we don't silently
        fall back."""
        home_dir = Path(os.environ["HOME"])
        cwd_dir = Path(cwd)
        home_md = home_dir / ".claude-bottle"
        cwd_md = cwd_dir / ".claude-bottle"

        _check_stale_json(home_dir, home_md, "$HOME")
        if cwd_dir.resolve() != home_dir.resolve():
            _check_stale_json(cwd_dir, cwd_md, "$CWD")

        if not home_md.is_dir():
            die(
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
                        f"live under $HOME/.claude-bottle/bottles/ "
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


def _validate_egress_routes(
    bottle_name: str,
    routes: tuple[EgressRoute, ...],
) -> None:
    """Cross-validation for `bottle.egress.routes`:

    - Hosts must be unique within the bottle. The proxy matches by
      exact-host (v1, prefix matching is on path_allowlist only);
      duplicate hosts leave the route choice ambiguous.
    - Singleton roles (see EGRESS_SINGLETON_ROLES) may appear
      on at most one route per bottle.

    No cross-validation against `bottle.git` is performed. git-gate
    (SSH push/fetch) and egress (HTTPS) broker different
    protocols; declaring both for the same host is a legitimate
    dev setup."""
    seen_hosts: dict[str, None] = {}
    for r in routes:
        key = r.Host.lower()
        if key in seen_hosts:
            die(
                f"bottle '{bottle_name}' egress.routes has duplicate host "
                f"{r.Host!r}; each host must be unique on the proxy."
            )
        seen_hosts[key] = None
    for role in EGRESS_SINGLETON_ROLES:
        with_role = [r for r in routes if role in r.Role]
        if len(with_role) > 1:
            hosts = ", ".join(r.Host for r in with_role)
            die(
                f"bottle '{bottle_name}' egress.routes has {len(with_role)} "
                f"routes with role {role!r} (hosts: {hosts}); this role drives a "
                f"single launch-step side effect — pick one."
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
    {"env", "git", "egress", "supervise"}
)
_AGENT_KEYS_REQUIRED = frozenset({"bottle"})
_AGENT_KEYS_OPTIONAL = frozenset({"skills"})
# Claude Code subagent fields claude-bottle ignores at launch but
# doesn't reject — lets the same file double as `~/.claude/agents/*.md`.
_AGENT_KEYS_CC_PASSTHROUGH = frozenset({
    "name", "description", "model", "color", "memory",
})
_AGENT_KEYS = (
    _AGENT_KEYS_REQUIRED | _AGENT_KEYS_OPTIONAL | _AGENT_KEYS_CC_PASSTHROUGH
)


def _check_stale_json(dir_path: Path, md_dir: Path, label: str) -> None:
    """Die if `<dir_path>/claude-bottle.json` exists but `md_dir` does
    not — the manifest format changed in PRD 0011 and we don't want
    to silently leave the JSON content unused."""
    legacy = dir_path / "claude-bottle.json"
    if legacy.is_file() and not md_dir.exists():
        die(
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
    hasn't declared any bottles yet)."""
    out: dict[str, Bottle] = {}
    if not bottles_dir.is_dir():
        return out
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
            die(f"could not read {path}: {e}")
        unknown = set(fm.keys()) - _BOTTLE_KEYS
        if unknown:
            allowed = ", ".join(sorted(_BOTTLE_KEYS))
            die(
                f"bottle file {path}: unknown frontmatter key(s) "
                f"{sorted(unknown)}; allowed keys are {allowed}."
            )
        out[name] = Bottle.from_dict(name, fm)
    return out


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
            die(f"could not read {path}: {e}")
        unknown = set(fm.keys()) - _AGENT_KEYS
        if unknown:
            allowed = ", ".join(sorted(_AGENT_KEYS))
            die(
                f"agent file {path}: unknown frontmatter key(s) "
                f"{sorted(unknown)}; allowed keys are {allowed}."
            )
        # Build the dict Agent.from_dict expects. The body becomes
        # prompt; CC passthrough fields stay in fm and get ignored
        # by from_dict (which only reads bottle/skills/prompt).
        agent_dict: dict[str, object] = {
            "bottle": fm.get("bottle"),
            "skills": fm.get("skills", []),
            "prompt": body.strip(),
        }
        out[name] = Agent.from_dict(name, agent_dict, bottle_names)
    return out
