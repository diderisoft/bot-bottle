"""Agent and bottle manifests: Markdown files with YAML frontmatter.

  $HOME/.bot-bottle/bottles/<name>.md   the user's bottles
  $HOME/.bot-bottle/agents/<name>.md    home agents
  $CWD/.bot-bottle/agents/<name>.md     project agents (trusted per host
                                        when their provider grants anything)
  $CWD/.bot-bottle/bottles/<name>.md    project bottles (trusted per host)

An agent's body is its system prompt; a bottle's body is documentation.

Bottle frontmatter:
  extends:      <bottle-name> | [<bottle-name>, ...]
  env:          { <NAME>: <env-entry>, ... }
  git-gate:
    user:       { name: <str>, email: <str> }
    repos:      { <name>: <git-gate-entry>, ... }
  egress:       { routes: [ <egress-route>, ... ] }
  supervise:    <bool>                          # default true
  nested_containers: <bool>                     # default false
  audit:        { delivery: ..., firehose: ... }

Agent frontmatter:
  bottle:       <bottle-name>                   # optional; else picked at launch
  skills:       [ <skill-name>, ... ]
  git-gate:
    user:       { name: <str>, email: <str> }   # overlays the bottle's
  remote:       { writable: <bool> }
  agent_provider: <harness> | { harness: ..., auth_token: ...,  # default claude
                  forward_host_credentials: ..., dockerfile: ...,
                  settings: {...}, overlay: {...} }
  # Claude Code subagent fields, accepted and ignored:
  name, description, model, color, memory

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
