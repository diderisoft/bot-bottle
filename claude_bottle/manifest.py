"""Manifest dataclasses. Read claude-bottle.json (cwd + $HOME, deep-merged)
into a frozen, validated Manifest tree.

Schema (see CLAUDE.md "Intended design"):
  {
    "bottles": {
      "<bottle-name>": {
        "env":     { "<NAME>": <env-entry>, ... },
        "git":     [ <git-entry>, ... ],
        "tokens":  [ <token-entry>, ... ],
        "egress":  { "allowlist": [ "<hostname>", ... ] }
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


TOKEN_KINDS = ("anthropic", "github", "gitea", "npm")


@dataclass(frozen=True)
class TokenEntry:
    """One credential the per-bottle cred-proxy sidecar (PRD 0010)
    holds and injects on the agent's behalf.

    `Kind` selects the route handler: `anthropic` / `github` / `npm`
    have fixed upstream URLs; `gitea` requires an explicit `Url`
    because the upstream is per-instance.

    `TokenRef` is the name of the host env var the CLI resolves at
    launch time. The value is forwarded into the cred-proxy
    container's environ via `docker run -e NAME` — never onto argv,
    never into a file. The value does NOT land in the agent's
    environ.

    `UpstreamHost` is parsed from `Url` for `gitea` entries (or the
    documented default for the other kinds). It exists so the
    cross-validator can spot collisions with `bottle.git` upstreams
    without re-parsing URLs at every call site."""

    Kind: str
    TokenRef: str
    Url: str = ""
    UpstreamHost: str = ""

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "TokenEntry":
        d = _as_json_object(raw, f"bottle '{bottle_name}' tokens[{idx}]")
        kind = d.get("Kind")
        if not isinstance(kind, str) or not kind:
            die(
                f"bottle '{bottle_name}' tokens[{idx}] missing required string field "
                f"'Kind'"
            )
        if kind not in TOKEN_KINDS:
            die(
                f"bottle '{bottle_name}' tokens[{idx}] Kind {kind!r} is not one of "
                f"{', '.join(TOKEN_KINDS)}"
            )
        token_ref = d.get("TokenRef")
        if not isinstance(token_ref, str) or not token_ref:
            die(
                f"bottle '{bottle_name}' tokens[{idx}] ({kind}) missing required "
                f"string field 'TokenRef' (name of the host env var to forward)"
            )
        url_raw = d.get("Url")
        if url_raw is None:
            url = ""
        elif isinstance(url_raw, str):
            url = url_raw
        else:
            die(
                f"bottle '{bottle_name}' tokens[{idx}] ({kind}) Url must be a string "
                f"(was {type(url_raw).__name__})"
            )
        if kind == "gitea":
            if not url:
                die(
                    f"bottle '{bottle_name}' tokens[{idx}] (gitea) requires a Url "
                    f"(the Gitea instance, e.g. https://gitea.dideric.is)"
                )
            host = _parse_https_host(
                url, f"bottle '{bottle_name}' tokens[{idx}] (gitea) Url"
            )
        else:
            if url:
                die(
                    f"bottle '{bottle_name}' tokens[{idx}] ({kind}) cannot set Url; "
                    f"the upstream for this Kind is fixed by cred-proxy. Drop the "
                    f"'Url' field."
                )
            host = _TOKEN_DEFAULT_HOST[kind]
        return cls(Kind=kind, TokenRef=token_ref, Url=url, UpstreamHost=host)


# Hostnames the cred-proxy talks to upstream for the non-gitea kinds.
# Used both for the proxy's route table and for the manifest cross-
# validator that rejects overlap with `bottle.git`.
_TOKEN_DEFAULT_HOST: dict[str, str] = {
    "anthropic": "api.anthropic.com",
    "github": "github.com",
    "npm": "registry.npmjs.org",
}


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
    tokens: tuple[TokenEntry, ...] = ()
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

        tokens: tuple[TokenEntry, ...] = ()
        tokens_raw = d.get("tokens")
        if tokens_raw is not None:
            if not isinstance(tokens_raw, list):
                die(
                    f"bottle '{name}' tokens must be an array "
                    f"(was {type(tokens_raw).__name__})"
                )
            tokens_list = cast(list[object], tokens_raw)
            tokens = tuple(
                TokenEntry.from_dict(name, i, entry)
                for i, entry in enumerate(tokens_list)
            )
            _validate_tokens(name, tokens, git)

        egress_raw = d.get("egress")
        egress = (
            BottleEgress.from_dict(name, egress_raw)
            if egress_raw is not None
            else BottleEgress()
        )

        return cls(env=env, git=git, tokens=tokens, egress=egress)


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
    Used to derive `TokenEntry.UpstreamHost` from a gitea Url so the
    cross-validator can spot collisions with `bottle.git` hosts."""
    if not url.startswith("https://"):
        die(f"{label} must be an https:// URL (was {url!r})")
    rest = url[len("https://"):]
    hostport, _, _ = rest.partition("/")
    host, _, _port = hostport.partition(":")
    if not host:
        die(f"{label} host is empty in {url!r}")
    return host


def _validate_tokens(
    bottle_name: str,
    tokens: tuple[TokenEntry, ...],
    git: tuple[GitEntry, ...],
) -> None:
    """Cross-validation for `bottle.tokens`:

    - At most one entry per Kind, except `gitea` which may have
      multiple entries (one per Gitea instance) with distinct Urls.
    - No overlap with `bottle.git` hosts: a `github` or `gitea` token
      whose host matches a `bottle.git` upstream host would put two
      credential brokers on the same remote (git-gate's gitleaks-
      scanning gate AND cred-proxy's bearer injection). Pick one.
    """
    by_kind: dict[str, list[TokenEntry]] = {}
    for t in tokens:
        by_kind.setdefault(t.Kind, []).append(t)
    for kind, entries in by_kind.items():
        if kind == "gitea":
            seen: dict[str, None] = {}
            for e in entries:
                if e.Url in seen:
                    die(
                        f"bottle '{bottle_name}' tokens has duplicate gitea Url "
                        f"{e.Url!r}; one entry per Gitea instance."
                    )
                seen[e.Url] = None
        elif len(entries) > 1:
            die(
                f"bottle '{bottle_name}' tokens has {len(entries)} entries with "
                f"Kind {kind!r}; at most one is allowed (gitea is the only Kind "
                f"that may have multiple entries)."
            )

    git_hosts = {g.UpstreamHost for g in git}
    for t in tokens:
        if t.Kind in ("github", "gitea") and t.UpstreamHost in git_hosts:
            die(
                f"bottle '{bottle_name}' token ({t.Kind}, host {t.UpstreamHost!r}) "
                f"overlaps a bottle.git upstream on the same host. git-gate already "
                f"brokers this remote; drop the token entry or remove the git entry."
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


