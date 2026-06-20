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
  supervise:    <bool>                          # optional

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

Validation runs once at load. Manifest.from_json_obj is preserved
as a programmatic entry point (used by tests) that takes a dict
with the same field names — useful for building manifests without
on-disk files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from .manifest_util import ManifestError, as_json_object
from .manifest_agent import ManifestAgent, ManifestAgentProvider
from .manifest_egress import (
    EGRESS_AUTH_SCHEMES,
    ManifestEgressConfig,
    ManifestEgressRoute,
)
from .manifest_git import ManifestGitEntry, ManifestGitUser, ManifestKeyConfig, parse_git_gate_config
from .manifest_schema import BOTTLE_KEYS

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
    "Manifest",
]


def _empty_str_dict() -> dict[str, str]:
    return {}


def _section_dict(value: object, label: str) -> dict[str, object]:
    """Like as_json_object but treats absent/null as an empty section."""
    if value is None:
        return {}
    return as_json_object(value, label)


@dataclass(frozen=True)
class ManifestBottle:
    env: Mapping[str, str] = field(default_factory=_empty_str_dict)
    agent_provider: ManifestAgentProvider = field(default_factory=ManifestAgentProvider)
    git: tuple[ManifestGitEntry, ...] = ()
    # Per-bottle git identity (issue #86). Empty default — bottles
    # that don't set `git-gate.user:` in the manifest skip the
    # `git config --global` step entirely. A bottle can declare a user
    # identity without any git-gate.repos upstreams, and vice versa.
    git_user: ManifestGitUser = field(default_factory=ManifestGitUser)
    egress: ManifestEgressConfig = field(default_factory=ManifestEgressConfig)
    # Opt-in per-bottle stuck-recovery sidecar (PRD 0013). When true,
    # the launch step brings up a supervise sidecar that exposes MCP
    # tools to the agent (egress-block, capability-block) plus mounts
    # the current-config dir read-only into the agent at
    # /etc/bot-bottle/current-config. False (the default) skips the
    # sidecar and mount.
    supervise: bool = False

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "ManifestBottle":
        d = as_json_object(raw, f"bottle '{name}'")

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
                f"(PRD 0009). Declare upstreams under 'git-gate.repos' with "
                f"url + identity + host_key; the git-gate sidecar (PRD 0008) "
                f"holds the credential and gitleaks-scans pushes."
            )

        if "git" in d:
            raise ManifestError(
                f"bottle '{name}' uses 'git' which has been replaced by "
                f"'git-gate' (PRD 0047). Move git.user → git-gate.user "
                f"and git.remotes → git-gate.repos (fields: url, identity, host_key)."
            )

        if "git_user" in d:
            raise ManifestError(
                f"bottle '{name}' has a 'git_user' field, which has been "
                f"removed. Move it under 'git-gate.user'."
            )

        unknown = set(d.keys()) - BOTTLE_KEYS
        if unknown:
            allowed = ", ".join(sorted(BOTTLE_KEYS))
            raise ManifestError(
                f"bottle '{name}' has unknown key(s) {sorted(unknown)}; "
                f"allowed keys are {allowed}."
            )

        env: dict[str, str] = {}
        env_raw = d.get("env")
        if env_raw is not None:
            env_dict = as_json_object(env_raw, f"bottle '{name}' env")
            for var, value in env_dict.items():
                if not isinstance(value, str):
                    raise ManifestError(
                        f"env entry {var} in bottle '{name}' must be a JSON string "
                        f"(was {type(value).__name__}). Use \"?<message>\" for prompt-at-runtime."
                    )
                env[var] = value

        git: tuple[ManifestGitEntry, ...] = ()
        git_user = ManifestGitUser()
        git_raw = d.get("git-gate")
        if git_raw is not None:
            git, git_user = parse_git_gate_config(name, git_raw)

        agent_provider = (
            ManifestAgentProvider.from_dict(name, d["agent_provider"])
            if "agent_provider" in d
            else ManifestAgentProvider()
        )

        egress = (
            ManifestEgressConfig.from_dict(name, d["egress"])
            if "egress" in d
            else ManifestEgressConfig()
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
class Manifest:
    bottles: Mapping[str, ManifestBottle]
    agents: Mapping[str, ManifestAgent]
    # Set by from_md_dirs; empty in from_json_obj (test/programmatic) mode.
    # Stores the manifest root dirs so load_for_agent can locate files later.
    home_md: Path | None = field(default=None)
    cwd_md: Path | None = field(default=None)

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

        from .manifest_loader import check_stale_json

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
    ) -> "Manifest":
        """Return a names-only Manifest. No file content is read; only
        filenames are scanned for the agent selector.  Full parsing happens
        later, per-agent, via `load_for_agent`.

        A `bottles/` subdir under `cwd_dir` is logged as a warning and
        ignored — the filesystem layout IS the trust boundary.

        Used by tests to build a Manifest from fixture directories
        without touching `os.environ`."""
        if cwd_dir is not None:
            stale_bottles = cwd_dir / "bottles"
            if stale_bottles.is_dir():
                files = sorted(stale_bottles.glob("*.md"))
                if files:
                    names = ", ".join(p.name for p in files)
                    from .log import warn
                    warn(
                        f"ignoring bottle file(s) under "
                        f"{stale_bottles}: {names}. Bottles can only "
                        f"live under $HOME/.bot-bottle/bottles/ "
                        f"(PRD 0011). Move them or delete."
                    )
        return cls(bottles={}, agents={}, home_md=home_dir, cwd_md=cwd_dir)

    @classmethod
    def from_json_obj(cls, obj: object) -> "Manifest":
        """Validate and build a Manifest from a raw JSON-like dict."""
        d = as_json_object(obj, "manifest")
        raw_bottles_obj = _section_dict(d.get("bottles"), "manifest 'bottles'")
        raw_agents = _section_dict(d.get("agents"), "manifest 'agents'")

        # Coerce each bottle's raw to dict[str, object] so the
        # PRD 0025 resolver can apply extends-merge rules
        # consistently with the md-loader path.
        raw_bottles: dict[str, dict[str, object]] = {}
        for n, b in raw_bottles_obj.items():
            raw_bottles[n] = as_json_object(b, f"bottle '{n}'")
        from .manifest_extends import resolve_bottles

        bottles = resolve_bottles(raw_bottles)

        bottle_names = set(bottles.keys())
        agents: dict[str, ManifestAgent] = {
            n: ManifestAgent.from_dict(n, a, bottle_names) for n, a in raw_agents.items()
        }
        return cls(bottles=bottles, agents=agents)

    @property
    def all_agent_names(self) -> list[str]:
        """Sorted list of all discoverable agent names.

        In names-only mode (from resolve/from_md_dirs) this scans agent
        filenames without reading their content. In eager mode (from
        from_json_obj) it returns the pre-parsed agents' names."""
        if self.home_md is not None:
            from .manifest_loader import scan_agent_names
            home_names = set(scan_agent_names(self.home_md / "agents").keys())
            cwd_names: set[str] = set()
            if self.cwd_md is not None:
                cwd_names = set(scan_agent_names(self.cwd_md / "agents").keys())
            return sorted(home_names | cwd_names)
        return sorted(self.agents.keys())

    def load_for_agent(self, agent_name: str) -> "Manifest":
        """Parse and return a full Manifest for `agent_name` and its bottle.

        Only the selected agent's file and the bottle files in its extends
        chain are read. Raises ManifestError if the agent or bottle is
        invalid. Must be called on a names-only manifest (from resolve).
        Backends call this at preflight to upgrade the spec's manifest."""
        if self.home_md is None:
            # Eager manifest (from_json_obj): already fully loaded; just validate name.
            if agent_name not in self.agents:
                available = ", ".join(sorted(self.agents.keys())) or "(none)"
                raise ManifestError(
                    f"agent '{agent_name}' not defined. Available: {available}"
                )
            return self
        from .manifest_loader import load_bottle_chain_from_dir, scan_agent_names
        from .manifest_schema import validate_agent_frontmatter_keys
        from .yaml_subset import YamlSubsetError, parse_frontmatter

        # Locate the agent file; cwd wins over home on name collision.
        home_agents = scan_agent_names(self.home_md / "agents")
        cwd_agents: dict[str, Path] = {}
        if self.cwd_md is not None:
            cwd_agents = scan_agent_names(self.cwd_md / "agents")
        merged = {**home_agents, **cwd_agents}

        if agent_name not in merged:
            available = ", ".join(sorted(merged.keys())) or "(none)"
            raise ManifestError(
                f"agent '{agent_name}' not defined. Available: {available}"
            )

        agent_path = merged[agent_name]
        try:
            fm, body = parse_frontmatter(agent_path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {agent_path}: {e}") from e
        except YamlSubsetError as e:
            raise ManifestError(f"{agent_path}: {e}") from e

        validate_agent_frontmatter_keys(agent_path, fm.keys())

        bottle_name = fm.get("bottle")
        if not isinstance(bottle_name, str) or not bottle_name:
            raise ManifestError(
                f"agent '{agent_name}' must declare a 'bottle' field "
                f"naming a defined bottle"
            )

        # Load the bottle chain (may raise ManifestError).
        bottles_dir = self.home_md / "bottles"
        bottle = load_bottle_chain_from_dir(bottle_name, bottles_dir)

        # Build and validate the full ManifestAgent.
        agent_dict: dict[str, object] = {
            "bottle": bottle_name,
            "skills": fm.get("skills", []),
            "prompt": body.strip(),
        }
        if "git-gate" in fm:
            agent_dict["git-gate"] = fm["git-gate"]
        agent = ManifestAgent.from_dict(agent_name, agent_dict, {bottle_name})

        return Manifest(
            bottles={bottle_name: bottle},
            agents={agent_name: agent},
            home_md=self.home_md,
            cwd_md=self.cwd_md,
        )

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

    def _effective_git_user(self, agent_name: str) -> ManifestGitUser:
        """Merge the agent's git.user over the referenced bottle's,
        per-field, agent-wins-on-non-empty (issue #94). Same overlay
        the `extends:` resolver applies between bottles
        (`_merge_bottles`)."""
        agent = self.agents[agent_name]
        base = self.bottles[agent.bottle].git_user
        over = agent.git_user
        if over.is_empty():
            return base
        return ManifestGitUser(
            name=over.name or base.name,
            email=over.email or base.email,
        )

    def bottle_for(self, agent_name: str) -> ManifestBottle:
        """Resolve the Bottle the named agent references, with the
        agent's git.user overlaid on top.

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
