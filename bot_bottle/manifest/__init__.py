"""Agent and bottle manifests: Markdown files with YAML frontmatter.

  $HOME/.bot-bottle/bottles/<name>.md   the user's bottles
  $HOME/.bot-bottle/agents/<name>.md    home agents
  $CWD/.bot-bottle/agents/<name>.md     project agents (trusted per host
                                        when their provider grants anything)
  $CWD/.bot-bottle/bottles/<name>.md    project bottles (trusted per host)

An agent's body is its system prompt; a bottle's body is documentation.

Bottle frontmatter:
  extends:      <bottle-name> | [<bottle-name>, ...]
  env:          { <NAME>: <value>, ... }   # literal, ?msg, ${VAR}, secret:NAME,
                                           # secret:?msg, secret:${VAR}
  git_gate:
    user:       { name: <str>, email: <str> }
    repos:      [ <forge>/<owner>/<repo>[:write], ... ] | { <name>: <entry>, ... }
    forges, trailers, protect_default_branch, actions
  egress:       { log: off|blocked|full, routes: [ <egress-route>, ... ] }
  supervise:    <bool>                          # default true
  nested_containers: <bool>                     # default false
  preserve_container: <bool>                    # default false: keep the guest across stop
  audit:        { delivery: ..., firehose: ... }
  resources:    { memory: <MiB | "4G">, cpus: <int> }  # default: the backend's size

An egress route (`manifest/egress.py`):
  host: <host>
  passthrough: true                             # alone: not inspected
  methods: [GET, POST] | ["*"]                  # default GET, HEAD
  allow: [ { paths: [/prefix/, {exact: /x}, {regex: ...}], methods, headers } ]
  auth: { type: bearer|token, value: secret:NAME | secret:${VAR} }
        | { type: header, name: <header>, value: ... }
  git: { fetch: true }
  preserve_auth: <bool>
  dlp: false | { outbound: [...], inbound: [...], on_match: supervise|redact|block }
  (or { host, blocked: true } to take an inherited route away)

Agent frontmatter (no `bottle`: bottles are picked at launch, `basic` if none):
  skills:       [ <skill-name>, ... ]
  identity:     { git: { name, email },         # overlays the bottle's git_gate.user
                  accounts: { <forge host>: <user> } }
  remote:       { writable: <bool> }
  agent_provider: <harness> | { harness: ...,   # default claude
                  login: host | { oauth_token: secret:NAME } | { api_key: secret:NAME },
                  dockerfile_path: ..., settings: {...}, overlay: {...} }
  # Claude Code subagent fields, accepted and ignored (`model` with a warning):
  name, description, model, color, memory

Old forms are refused, naming their new form (`util.OldFormError`).
Unknown keys raise ManifestError. `ManifestIndex` is the collection used for
selection; `Manifest` is one agent with its effective bottle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .index import Manifest, ManifestIndex
    from .util import ManifestError
    from .agent import ManifestAgent, ManifestAgentProvider, ManifestImageOverlay
    from .bottle import ManifestAudit, ManifestBottle
    from .egress import EGRESS_AUTH_SCHEMES, ManifestEgressConfig, ManifestEgressRoute
    from .git import ManifestGitEntry, ManifestGitUser, ManifestKeyConfig


# Facade name -> defining submodule.
_LAZY_MODULES: dict[str, str] = {
    "Manifest": "index",
    "ManifestIndex": "index",
    "ManifestError": "util",
    "ManifestAgent": "agent",
    "ManifestAgentProvider": "agent",
    "ManifestImageOverlay": "agent",
    "ManifestAudit": "bottle",
    "ManifestBottle": "bottle",
    "EGRESS_AUTH_SCHEMES": "egress",
    "ManifestEgressRoute": "egress",
    "ManifestEgressConfig": "egress",
    "ManifestGitEntry": "git",
    "ManifestGitUser": "git",
    "ManifestKeyConfig": "git",
}


def __getattr__(name: str) -> Any:
    """Resolve a facade name from its submodule on first access and cache it."""
    mod = _LAZY_MODULES.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{mod}"), name)
    globals()[name] = value
    return value


__all__ = [
    "Manifest",
    "ManifestIndex",
    "ManifestError",
    "ManifestGitEntry",
    "ManifestGitUser",
    "ManifestKeyConfig",
    "ManifestAgentProvider",
    "ManifestImageOverlay",
    "ManifestAgent",
    "ManifestAudit",
    "ManifestBottle",
    "EGRESS_AUTH_SCHEMES",
    "ManifestEgressRoute",
    "ManifestEgressConfig",
]
