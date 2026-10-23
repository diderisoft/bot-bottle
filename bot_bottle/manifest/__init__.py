"""Manifest dataclasses.

Reads the per-file manifest tree:

  $HOME/.bot-bottle/bottles/<name>.md   — one bottle per file
  $HOME/.bot-bottle/agents/<name>.md    — home-resident agents
  $CWD/.bot-bottle/agents/<name>.md     — cwd-supplied agents

Each file is Markdown with YAML frontmatter. The frontmatter holds
the structured config (see schema below); for agents the body is
the system prompt, for bottles the body is human documentation
(ignored by the parser).

Bottle schema (frontmatter):
  extends:      <bottle-name>                   # optional
  env:          { <NAME>: <env-entry>, ... }
  git-gate:                                     # optional
    user:       { name: <str>, email: <str> }   # optional
    repos:      { <name>: <git-gate-entry>, ... }  # optional
  egress: { routes: [ <egress-route>, ... ] }
    # route keys: host, matches, auth, role, dlp
  supervise:    <bool>                          # optional (default true)
  nested_containers: <bool>                     # optional (default false)

Agent schema (frontmatter):
  bottle:        <bottle-name>          # required
  skills:        [ <skill-name>, ... ]   # optional
  git-gate:
    user:        { name: <str>, email: <str> }  # optional; overlays bottle
  # Claude Code subagent passthrough fields — accepted, ignored:
  name, description, model, color, memory

The agent file's Markdown body is the system prompt (stripped).
Unknown top-level frontmatter keys raise ManifestError with a hint.

Bottles can ONLY live under $HOME. A bottles/ dir under $CWD is a
warn at load time and contributes nothing. The trust boundary is
expressed as filesystem layout rather than resolver logic.

Two types are exported:

  ManifestIndex  — the multi-agent/bottle collection returned by
                   resolve() and from_json_obj(). Used for agent
                   selection (all_agent_names), validation
                   (require_agent), and lazy loading (load_for_agent).
                   This is the pre-preflight form.

  Manifest       — a single-agent/bottle value type holding exactly
                   one agent: ManifestAgent and one bottle:
                   ManifestBottle (with the agent's git-gate.user
                   already overlaid). Returned by load_for_agent().
                   This is the post-preflight form passed to backends.

ManifestIndex.from_json_obj is preserved as a programmatic entry
point (used by tests) that takes a dict with the same field names —
useful for building manifests without on-disk files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .index import Manifest, ManifestIndex
    from .util import ManifestError
    from .agent import ManifestAgent, ManifestAgentProvider, ManifestImageOverlay
    from .bottle import ManifestBottle
    from .egress import EGRESS_AUTH_SCHEMES, ManifestEgressConfig, ManifestEgressRoute
    from .git import ManifestGitEntry, ManifestGitUser, ManifestKeyConfig


# Facade name -> submodule that defines it. The aggregate model (`Manifest`,
# `ManifestIndex`) lives in `index`; the piece types in their own modules.
_LAZY_MODULES: dict[str, str] = {
    "Manifest": "index",
    "ManifestIndex": "index",
    "ManifestError": "util",
    "ManifestAgent": "agent",
    "ManifestAgentProvider": "agent",
    "ManifestImageOverlay": "agent",
    "ManifestBottle": "bottle",
    "EGRESS_AUTH_SCHEMES": "egress",
    "ManifestEgressRoute": "egress",
    "ManifestEgressConfig": "egress",
    "ManifestGitEntry": "git",
    "ManifestGitUser": "git",
    "ManifestKeyConfig": "git",
}


def __getattr__(name: str) -> Any:
    """Lazily surface the manifest facade names from their submodules and cache
    them at package level — so importing a leaf (e.g. `manifest.util`) doesn't
    build the whole manifest model, while `from bot_bottle.manifest import
    Manifest` keeps working."""
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
    "ManifestBottle",
    "EGRESS_AUTH_SCHEMES",
    "ManifestEgressRoute",
    "ManifestEgressConfig",
]
