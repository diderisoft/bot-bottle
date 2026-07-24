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
  git-gate:                                     # optional (PRD 0047)
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

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from ..log import warn
from .util import ManifestError, as_json_object
from .agent import ManifestAgent, ManifestAgentProvider
from .bottle import ManifestBottle
from .egress import (
    EGRESS_AUTH_SCHEMES,
    ManifestEgressConfig,
    ManifestEgressRoute,
)
from .extends import merge_bottles_runtime, resolve_bottles
from .git import ManifestGitEntry, ManifestGitUser, ManifestKeyConfig
from .loader import (
    check_stale_json,
    load_bottle_chain_from_dir,
    scan_agent_names,
    scan_bottle_names,
)
from .schema import validate_agent_frontmatter_keys
from ..yaml_subset import YamlSubsetError, parse_frontmatter

# Re-export everything that callers currently import from this module.
__all__ = [
    "ManifestError",
    "ManifestGitEntry",
    "ManifestGitUser",
    "ManifestKeyConfig",
    "ManifestAgentProvider",
    "EGRESS_AUTH_SCHEMES",
    "ManifestEgressRoute",
    "ManifestEgressConfig",
    "ManifestAgent",
    "ManifestBottle",
    "ManifestIndex",
    "Manifest",
]


def _section_dict(value: object, label: str) -> dict[str, object]:
    """Like as_json_object but treats absent/null as an empty section."""
    if value is None:
        return {}
    return as_json_object(value, label)


def _merge_git_user(
    agent_user: ManifestGitUser, base_user: ManifestGitUser
) -> ManifestGitUser:
    """Merge the agent's git.user over the bottle's, agent-wins-on-non-empty."""
    if agent_user.is_empty():
        return base_user
    return ManifestGitUser(
        name=agent_user.name or base_user.name,
        email=agent_user.email or base_user.email,
    )


def _manifest_with_merged_git_user(
    agent: "ManifestAgent", raw_bottle: "ManifestBottle"
) -> "Manifest":
    """Build the single-value Manifest, overlaying the agent's git-gate.user
    onto the bottle (agent wins on non-empty, per-field). Shared by the eager
    and lazy load_for_agent paths."""
    merged = _merge_git_user(agent.git_user, raw_bottle.git_user)
    bottle = (
        raw_bottle if merged == raw_bottle.git_user
        else replace(raw_bottle, git_user=merged)
    )
    return Manifest(agent=agent, bottle=bottle)


def _resolve_effective_bottle_eager(
    agent_name: str,
    agent: "ManifestAgent",
    bottle_names: "tuple[str, ...]",
    bottles: "Mapping[str, ManifestBottle]",
) -> "ManifestBottle":
    """Return the effective ManifestBottle for the eager (from_json_obj) path.

    When bottle_names is non-empty they are merged in order. When empty, falls
    back to agent.bottle. Raises ManifestError when neither is set."""
    if bottle_names:
        resolved: list[ManifestBottle] = []
        for bn in bottle_names:
            if bn not in bottles:
                available = ", ".join(sorted(bottles.keys())) or "(none)"
                raise ManifestError(
                    f"bottle '{bn}' not defined. Available: {available}"
                )
            resolved.append(bottles[bn])
        return merge_bottles_runtime(resolved)

    if not agent.bottle:
        raise ManifestError(
            f"agent '{agent_name}' has no 'bottle' field and no bottles were "
            f"selected at launch. Select at least one bottle or add "
            f"'bottle: <name>' to the agent manifest."
        )
    return bottles[agent.bottle]


def _resolve_effective_bottle_lazy(
    agent_name: str,
    agent_bottle: str,
    bottle_names: "tuple[str, ...]",
    bottles_dir: "Path",
) -> "ManifestBottle":
    """Return the effective ManifestBottle for the lazy (from_md_dirs) path.

    When bottle_names is non-empty they are resolved from disk and merged in
    order. When empty, falls back to agent_bottle. Raises ManifestError when
    neither is set."""
    if bottle_names:
        resolved = [load_bottle_chain_from_dir(bn, bottles_dir) for bn in bottle_names]
        return merge_bottles_runtime(resolved)

    if not agent_bottle:
        raise ManifestError(
            f"agent '{agent_name}' has no 'bottle' field and no bottles were "
            f"selected at launch. Select at least one bottle or add "
            f"'bottle: <name>' to the agent manifest."
        )
    return load_bottle_chain_from_dir(agent_bottle, bottles_dir)


@dataclass(frozen=True)
class Manifest:
    """Single-agent/bottle value type. Returned by ManifestIndex.load_for_agent().

    `bottle` is the effective bottle with the agent's git-gate.user already
    overlaid per-field (agent wins on non-empty). Backends and provisioners
    use this directly — no agent_name lookup needed."""

    agent: ManifestAgent
    bottle: ManifestBottle

    def git_identity_summary(self) -> str | None:
        """One-line effective git identity with per-field provenance, e.g.
        `name=claude (agent), email=eric@dideric.is (bottle)`.
        Returns None when neither agent nor bottle sets an identity."""
        over = self.agent.git_user        # agent's declared git_user (pre-merge)
        merged = self.bottle.git_user     # effective git_user (post-merge)
        if merged.is_empty():
            return None
        parts: list[str] = []
        if merged.name:
            parts.append(f"name={merged.name} ({'agent' if over.name else 'bottle'})")
        if merged.email:
            parts.append(f"email={merged.email} ({'agent' if over.email else 'bottle'})")
        return ", ".join(parts)


@dataclass(frozen=True)
class ManifestIndex:
    """Multi-agent/bottle collection. The pre-preflight form.

    In lazy mode (from resolve()/from_md_dirs()) only filenames are scanned;
    no file content is read. In eager mode (from from_json_obj()) all agents
    and bottles are pre-parsed. Call load_for_agent() to get a single-value
    Manifest ready for backend use."""

    bottles: Mapping[str, ManifestBottle]
    agents: Mapping[str, ManifestAgent]
    # Set by from_md_dirs; None in from_json_obj (test/programmatic) mode.
    # Stores the manifest root dirs so load_for_agent can locate files later.
    home_md: Path | None = field(default=None)
    cwd_md: Path | None = field(default=None)

    @classmethod
    def resolve(cls, cwd: str, *, missing_ok: bool = False) -> "ManifestIndex":
        """Walk the per-file manifest tree and build a ManifestIndex.

        Layout (PRD 0011):
          $HOME/.bot-bottle/bottles/<name>.md  — bottles (home-only)
          $HOME/.bot-bottle/agents/<name>.md   — home agents
          $CWD/.bot-bottle/agents/<name>.md    — cwd agents

        Cwd agents merge into the home agents on the same name
        (cwd wins). A bottles/ subdir under $CWD is logged as a
        warning and ignored — the filesystem layout IS the trust
        boundary.

        If `missing_ok` is true, a missing `$HOME/.bot-bottle/`
        returns an empty index instead of dying. This is for
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

        check_stale_json(home_dir, home_md, "$HOME")
        if cwd_dir.resolve() != home_dir.resolve():
            check_stale_json(cwd_dir, cwd_md, "$CWD")

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
    ) -> "ManifestIndex":
        """Return a names-only ManifestIndex. No file content is read; only
        filenames are scanned for the agent selector.  Full parsing happens
        later, per-agent, via `load_for_agent`.

        A `bottles/` subdir under `cwd_dir` is logged as a warning and
        ignored — the filesystem layout IS the trust boundary.

        Used by tests to build a ManifestIndex from fixture directories
        without touching `os.environ`."""
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
        return cls(bottles={}, agents={}, home_md=home_dir, cwd_md=cwd_dir)

    @classmethod
    def from_json_obj(cls, obj: object) -> "ManifestIndex":
        """Validate and build a ManifestIndex from a raw JSON-like dict."""
        d = as_json_object(obj, "manifest")
        raw_bottles_obj = _section_dict(d.get("bottles"), "manifest 'bottles'")
        raw_agents = _section_dict(d.get("agents"), "manifest 'agents'")

        # Coerce each bottle's raw to dict[str, object] so the
        # PRD 0025 resolver can apply extends-merge rules
        # consistently with the md-loader path.
        raw_bottles: dict[str, dict[str, object]] = {}
        for n, b in raw_bottles_obj.items():
            raw_bottles[n] = as_json_object(b, f"bottle '{n}'")

        bottles = resolve_bottles(raw_bottles)

        bottle_names = set(bottles.keys())
        agents: dict[str, ManifestAgent] = {
            n: ManifestAgent.from_dict(n, a, bottle_names) for n, a in raw_agents.items()
        }
        return cls(bottles=bottles, agents=agents)

    @property
    def all_bottle_names(self) -> list[str]:
        """Sorted list of all discoverable bottle names.

        In names-only mode (from resolve/from_md_dirs) this scans bottle
        filenames without reading their content. In eager mode (from
        from_json_obj) it returns the pre-parsed bottles' names."""
        if self.home_md is not None:
            return scan_bottle_names(self.home_md / "bottles")
        return sorted(self.bottles.keys())

    @property
    def all_agent_names(self) -> list[str]:
        """Sorted list of all discoverable agent names.

        In names-only mode (from resolve/from_md_dirs) this scans agent
        filenames without reading their content. In eager mode (from
        from_json_obj) it returns the pre-parsed agents' names."""
        if self.home_md is not None:
            home_names = set(scan_agent_names(self.home_md / "agents").keys())
            cwd_names: set[str] = set()
            if self.cwd_md is not None:
                cwd_names = set(scan_agent_names(self.cwd_md / "agents").keys())
            return sorted(home_names | cwd_names)
        return sorted(self.agents.keys())

    def load_for_agent(
        self,
        agent_name: str,
        bottle_names: "tuple[str, ...] | None" = None,
    ) -> "Manifest":
        """Parse the named agent and its bottle; return a single-value Manifest.

        `bottle_names` is an ordered list of bottles selected at launch time.
        When non-empty they are resolved and merged in order (index 0 = base;
        later entries override). When empty or None, falls back to the agent's
        own `bottle:` field. Raises ManifestError when neither is set.

        In lazy mode (from resolve/from_md_dirs) the agent file and its
        bottle chain are read from disk for the first time here.  In eager
        mode (from_json_obj) the data is already parsed; this just filters
        down to the requested agent and its bottle.

        The returned Manifest.bottle has the agent's git-gate.user already
        overlaid (agent wins on non-empty, per-field).

        Always raises ManifestError if the agent is unknown or invalid.
        Backends call this at preflight inside _validate."""
        effective_bottle_names: tuple[str, ...] = bottle_names or ()
        if self.home_md is None:
            return self._load_for_agent_eager(agent_name, effective_bottle_names)
        return self._load_for_agent_lazy(agent_name, effective_bottle_names)

    def _load_for_agent_eager(
        self, agent_name: str, bottle_names: tuple[str, ...]
    ) -> "Manifest":
        """Eager path (from_json_obj): data is already parsed; filter to the one
        requested agent and its bottle so the returned Manifest always holds
        exactly one agent and one bottle regardless of path."""
        if agent_name not in self.agents:
            available = ", ".join(sorted(self.agents.keys())) or "(none)"
            raise ManifestError(
                f"agent '{agent_name}' not defined. Available: {available}"
            )
        agent = self.agents[agent_name]
        raw_bottle = _resolve_effective_bottle_eager(
            agent_name, agent, bottle_names, self.bottles
        )
        return _manifest_with_merged_git_user(agent, raw_bottle)

    def _load_for_agent_lazy(
        self, agent_name: str, bottle_names: tuple[str, ...]
    ) -> "Manifest":
        """Lazy path (resolve/from_md_dirs): read and parse the agent file and
        its bottle chain from disk for the first time here."""
        assert self.home_md is not None  # guaranteed by load_for_agent dispatch
        # Locate the agent file; cwd wins over home on name collision.
        home_agents = scan_agent_names(self.home_md / "agents")
        cwd_agents: dict[str, Path] = {}
        if self.cwd_md is not None:
            cwd_agents = scan_agent_names(self.cwd_md / "agents")
        merged_agents = {**home_agents, **cwd_agents}

        if agent_name not in merged_agents:
            available = ", ".join(sorted(merged_agents.keys())) or "(none)"
            raise ManifestError(
                f"agent '{agent_name}' not defined. Available: {available}"
            )

        agent_path = merged_agents[agent_name]
        try:
            fm, body = parse_frontmatter(agent_path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {agent_path}: {e}") from e
        except YamlSubsetError as e:
            raise ManifestError(f"{agent_path}: {e}") from e

        validate_agent_frontmatter_keys(agent_path, fm.keys())

        # Determine the effective bottle name(s).
        agent_bottle = fm.get("bottle") or ""
        bottles_dir = self.home_md / "bottles"
        raw_bottle = _resolve_effective_bottle_lazy(
            agent_name, str(agent_bottle), bottle_names, bottles_dir
        )
        effective_bottle_name = (
            bottle_names[-1] if bottle_names else str(agent_bottle)
        )

        # Build and validate the full ManifestAgent.
        agent_dict: dict[str, object] = {
            "skills": fm.get("skills", []),
            "prompt": body.strip(),
        }
        if agent_bottle:
            agent_dict["bottle"] = agent_bottle
        if "git-gate" in fm:
            agent_dict["git-gate"] = fm["git-gate"]
        # Pass the effective bottle name as the known-bottles set so agents
        # that have bottle: set are validated; agents without bottle: pass {}
        # since bottle_names were already resolved above.
        known = {effective_bottle_name} if effective_bottle_name else set()
        agent = ManifestAgent.from_dict(agent_name, agent_dict, known)

        return _manifest_with_merged_git_user(agent, raw_bottle)

    def has_agent(self, name: str) -> bool:
        return name in self.agents

    def require_agent(self, name: str) -> None:
        """Check that `name` is a discoverable agent. In names-only mode
        this checks whether the .md file exists; in eager mode it checks
        the pre-parsed agents dict. Does NOT parse file content."""
        if self.has_agent(name):
            return
        if self.home_md is not None:
            # Names-only mode: check file existence without parsing.
            home_path = self.home_md / "agents" / f"{name}.md"
            cwd_path = (
                self.cwd_md / "agents" / f"{name}.md"
                if self.cwd_md else None
            )
            if home_path.is_file() or (cwd_path and cwd_path.is_file()):
                return
        available = ", ".join(self.all_agent_names) or "(none)"
        raise ManifestError(
            f"agent '{name}' not defined. Available: {available}"
        )
