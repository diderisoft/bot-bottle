"""The `Manifest` and `ManifestIndex` models and the bottle-resolution helpers
that combine the manifest pieces into an effective manifest.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Sequence
from typing import Mapping

from .util import ManifestError, as_json_object
from ..agent_provider import harness_ready
from .agent import ManifestAgent, agent_from_frontmatter
from .bottle import ManifestBottle
from .extends import merge_bottles_runtime, resolve_bottles
from .git import ManifestGitUser
from .loader import (
    BUILTIN_AGENTS_DIR,
    BUILTIN_BOTTLES_DIR,
    BottleSource,
    agent_listed,
    bottle_ref,
    check_stale_json,
    load_bottle_chain,
    scan_agent_names,
    scan_bottle_names,
)
from ..paths import bot_bottle_root, project_manifest_dir
from .trust import check as trust_check
from .trust import check_agent as trust_check_agent
from .forges import with_forge_users
from .schema import DEFAULT_BOTTLE, validate_agent_frontmatter_keys
from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter


def _section_dict(value: object, label: str) -> dict[str, object]:
    """Like as_json_object but treats absent/null as an empty section."""
    if value is None:
        return {}
    return as_json_object(value, label)


def _merge_git_user(
    agent_user: ManifestGitUser, base_user: ManifestGitUser
) -> ManifestGitUser:
    """The agent's git user over the bottle's, agent winning when non-empty."""
    if agent_user.is_empty():
        return base_user
    return ManifestGitUser(
        name=agent_user.name or base_user.name,
        email=agent_user.email or base_user.email,
    )


def _manifest_with_merged_git_user(
    agent: "ManifestAgent", raw_bottle: "ManifestBottle"
) -> "Manifest":
    """The single-agent Manifest, with the agent's git_gate.user overlaid on
    the bottle's per field, and its repos on the forges it names in
    `identity.accounts` pushing as those users."""
    merged = _merge_git_user(agent.git_user, raw_bottle.git_user)
    bottle = (
        raw_bottle if merged == raw_bottle.git_user
        else replace(raw_bottle, git_user=merged)
    )
    if agent.forge_users:
        bottle = with_forge_users(bottle, dict(agent.forge_users))
    return Manifest(agent=agent, bottle=bottle)


def _resolve_effective_bottle_eager(
    bottle_names: "tuple[str, ...]",
    bottles: "Mapping[str, ManifestBottle]",
) -> "ManifestBottle":
    """The effective bottle (eager path): `bottle_names` merged in order, else
    `DEFAULT_BOTTLE`."""
    if not bottle_names and DEFAULT_BOTTLE not in bottles:
        return load_bottle_chain(DEFAULT_BOTTLE, [(BUILTIN_BOTTLES_DIR, False)])[0]
    resolved: list[ManifestBottle] = []
    for bn in bottle_names or (DEFAULT_BOTTLE,):
        if bn not in bottles:
            available = ", ".join(sorted(bottles.keys())) or "(none)"
            raise ManifestError(
                f"bottle '{bn}' not defined. Available: {available}"
            )
        resolved.append(bottles[bn])
    return merge_bottles_runtime(resolved)


def _load_checked(
    name: str, dirs: "Sequence[tuple[Path, bool]]",
    forge_users: "Mapping[str, str] | None" = None,
) -> "tuple[ManifestBottle, tuple[BottleSource, ...]]":
    """Load one bottle chain; one with project files must be trusted, as
    the accounts the agent's `identity.accounts` picks, so trusting it covers whom
    its grants push as."""
    bottle, sources = load_bottle_chain(name, dirs)
    checked = with_forge_users(bottle, dict(forge_users)) if forge_users else bottle
    trust_check(name, checked, sources)
    return bottle, sources


def _resolve_effective_bottle_lazy(
    bottle_names: "tuple[str, ...]",
    dirs: "Sequence[tuple[Path, bool]]",
    forge_users: "Mapping[str, str] | None" = None,
) -> "tuple[ManifestBottle, tuple[BottleSource, ...]]":
    """The effective bottle (lazy path) and its files, resolved from disk:
    `bottle_names` merged in order, else `DEFAULT_BOTTLE`. Raises
    UntrustedBottleError for an untrusted project bottle."""
    if not bottle_names:
        return _load_checked(DEFAULT_BOTTLE, dirs, forge_users)
    loaded = [_load_checked(bn, dirs, forge_users) for bn in bottle_names]
    return (merge_bottles_runtime([b for b, _ in loaded]),
            tuple(s for _, sources in loaded for s in sources))


@dataclass(frozen=True)
class Manifest:
    """One agent and its effective bottle, the agent's git_gate.user already
    overlaid. What backends use."""

    agent: ManifestAgent
    bottle: ManifestBottle

    def git_identity_summary(self) -> str | None:
        """The effective git identity with provenance, e.g. `name=claude
        (agent), email=eric@dideric.is (bottle)`; None when unset."""
        over = self.agent.git_user  # agent's own git_user
        merged = self.bottle.git_user  # effective git_user
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
    """All agents and bottles. Lazy mode (`resolve`, `from_md_dirs`) only scans
    filenames; eager mode (`from_json_obj`) pre-parses everything.
    `load_for_agent` gives a single-agent Manifest."""

    bottles: Mapping[str, ManifestBottle]
    agents: Mapping[str, ManifestAgent]
    # Manifest root, set by from_md_dirs.
    home_md: Path | None = field(default=None)
    cwd_md: Path | None = field(default=None)
    # Bottles only this launch sees, searched first: the architect's own
    # bottle, or a run's unsaved one. Never part of the library.
    extra_bottle_dirs: tuple[Path, ...] = field(default=())
    # Agents searched before the host's: a library commit's (`with_library`).
    extra_agent_dirs: tuple[Path, ...] = field(default=())

    @classmethod
    def resolve(cls, cwd: str, *, missing_ok: bool = False) -> "ManifestIndex":
        """Build a ManifestIndex from the per-file tree:

          $HOME/.bot-bottle/bottles/<name>.md  user bottles ($BOT_BOTTLE_ROOT
          $HOME/.bot-bottle/agents/<name>.md   home agents   moves both)
          $CWD/.bot-bottle/agents/<name>.md    cwd agents (win on a name clash)
          $CWD/.bot-bottle/bottles/<name>.md   project bottles (need trust)
          built-in bottles (`basic`)

        Bottles resolve from the user's, then the project's, then built-ins, so
        a project can't replace a user bottle. `missing_ok` returns an empty
        index without `$HOME/.bot-bottle/`. A leftover `bot-bottle.json` dies
        with a pointer to the per-file format."""
        home_dir = Path(os.environ["HOME"])
        cwd_dir = Path(cwd)
        home_md = bot_bottle_root(home_dir)
        cwd_md = cwd_dir / ".bot-bottle"

        check_stale_json(home_dir, home_md, "$HOME")
        if cwd_dir.resolve() != home_dir.resolve():
            check_stale_json(cwd_dir, cwd_md, "$CWD")

        if not home_md.is_dir():
            if missing_ok:
                return cls.from_json_obj({"bottles": {}, "agents": {}})
            raise ManifestError(
                f"no manifest found: {home_md} does not exist. "
                f"See docs/reference/manifest.md for the per-file Markdown layout."
            )

        return cls.from_md_dirs(home_md, project_manifest_dir(cwd_dir, home=home_dir))

    @classmethod
    def from_md_dirs(
        cls,
        home_dir: Path,
        cwd_dir: Path | None,
    ) -> "ManifestIndex":
        """A names-only ManifestIndex; files are parsed later by
        `load_for_agent`. Project bottles load only once trusted. Lets tests
        use fixture dirs without `os.environ`."""
        return cls(bottles={}, agents={}, home_md=home_dir, cwd_md=cwd_dir)

    @classmethod
    def from_json_obj(cls, obj: object) -> "ManifestIndex":
        """Validate and build a ManifestIndex from a JSON-like dict."""
        d = as_json_object(obj, "manifest")
        raw_bottles_obj = _section_dict(d.get("bottles"), "manifest 'bottles'")
        raw_agents = _section_dict(d.get("agents"), "manifest 'agents'")

        # Raw dicts, so extends merges as on the md path.
        raw_bottles: dict[str, dict[str, object]] = {}
        for n, b in raw_bottles_obj.items():
            raw_bottles[n] = as_json_object(b, f"bottle '{n}'")

        bottles = resolve_bottles(raw_bottles)

        agents: dict[str, ManifestAgent] = {
            n: ManifestAgent.from_dict(n, a) for n, a in raw_agents.items()
        }
        return cls(bottles=bottles, agents=agents)

    def with_library(self, tree: Path) -> "ManifestIndex":
        """This index resolving library bottles and agents from `tree` (a
        library commit's `agents/` and `bottles/`) before the host's own,
        which then answers only for names `tree` lacks."""
        return replace(
            self, extra_bottle_dirs=(*self.extra_bottle_dirs, tree / "bottles"),
            extra_agent_dirs=(tree / "agents", *self.extra_agent_dirs))

    @property
    def all_bottle_names(self) -> list[str]:
        """Sorted bottle names (filenames in lazy mode)."""
        if self.home_md is not None:
            return sorted({bottle_ref(n, project) for d, project in self.bottle_dirs()
                           for n in scan_bottle_names(d)})
        return sorted(self.bottles.keys())

    def bottle_dirs(self) -> list[tuple[Path, bool]]:
        """Bottle directories in precedence order, as `(directory,
        is_project)`."""
        assert self.home_md is not None
        dirs = [(d, False) for d in self.extra_bottle_dirs]
        dirs.append((self.home_md / "bottles", False))
        if self.cwd_md is not None:
            dirs.append((self.cwd_md / "bottles", True))
        dirs.append((BUILTIN_BOTTLES_DIR, False))
        return dirs

    @property
    def all_agent_names(self) -> list[str]:
        """Sorted agent names offered to pick one to launch (filenames in lazy
        mode), built-ins included, those marked `listed: false` left out."""
        if self.home_md is not None:
            return sorted(self.agent_files(listing=True))
        return sorted(n for n, a in self.agents.items() if a.listed)

    def has_agent_named(self, name: str) -> bool:
        """Whether `name` resolves to an agent, listed or not."""
        if self.home_md is not None:
            return name in self.agent_files()
        return name in self.agents

    def agent_files(self, *, listing: bool = False) -> dict[str, Path]:
        """Each agent name's file, lazy mode: a project's over the host's over
        the built-ins (`BUILTIN_AGENTS_DIR`). A built-in is named by its
        harness; `listing` keeps only those whose harness is ready here, so
        what's offered can run, and leaves out any file marked `listed:
        false` (one named outright still resolves)."""
        assert self.home_md is not None
        files = scan_agent_names(self.home_md / "agents")
        for directory in reversed(self.extra_agent_dirs):
            files.update(scan_agent_names(directory))
        if self.cwd_md is not None:
            files.update(scan_agent_names(self.cwd_md / "agents"))
        builtins = {n: p for n, p in scan_agent_names(BUILTIN_AGENTS_DIR).items()
                    if n not in files and (not listing or (
                        agent_listed(p) and harness_ready(n)))}
        if listing:
            files = {n: p for n, p in files.items() if agent_listed(p)}
        return {**builtins, **files}

    def host_agent(self, name: str) -> Path | None:
        """The agent a project agent `name` replaces: the library commit's,
        else the host's, else the built-in. Lazy mode."""
        assert self.home_md is not None
        for directory in (*self.extra_agent_dirs, self.home_md / "agents", BUILTIN_AGENTS_DIR):
            found = scan_agent_names(directory).get(name)
            if found is not None:
                return found
        return None

    def load_for_agent(
        self,
        agent_name: str,
        bottle_names: "tuple[str, ...] | None" = None,
    ) -> "Manifest":
        """Parse the agent and its bottle into a single-agent Manifest.
        `bottle_names` are merged in order (0 is the base); empty means
        `DEFAULT_BOTTLE`. The bottle has the agent's git_gate.user
        overlaid. Raises ManifestError for an unknown or invalid agent or
        bottle."""
        effective_bottle_names: tuple[str, ...] = bottle_names or ()
        if self.home_md is None:
            return self._load_for_agent_eager(agent_name, effective_bottle_names)
        return self._load_for_agent_lazy(agent_name, effective_bottle_names)

    def _load_for_agent_eager(
        self, agent_name: str, bottle_names: tuple[str, ...]
    ) -> "Manifest":
        """Eager path: filter the parsed data to the one agent and its bottle."""
        if agent_name not in self.agents:
            available = ", ".join(sorted(self.agents.keys())) or "(none)"
            raise ManifestError(
                f"agent '{agent_name}' not defined. Available: {available}"
            )
        agent = self.agents[agent_name].resolved(agent_name)
        raw_bottle = _resolve_effective_bottle_eager(bottle_names, self.bottles)
        return _manifest_with_merged_git_user(agent, raw_bottle)

    def _load_for_agent_lazy(
        self, agent_name: str, bottle_names: tuple[str, ...]
    ) -> "Manifest":
        """Lazy path: read and parse the agent file and its bottle chain."""
        assert self.home_md is not None  # set by load_for_agent's dispatch
        # cwd wins over home, and home over the built-ins, on a name clash.
        cwd_agents: dict[str, Path] = {}
        if self.cwd_md is not None:
            cwd_agents = scan_agent_names(self.cwd_md / "agents")
        merged_agents = self.agent_files()

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

        agent = agent_from_frontmatter(agent_name, dict(fm), body).resolved(agent_name)
        raw_bottle, bottle_sources = _resolve_effective_bottle_lazy(
            bottle_names, self.bottle_dirs(), dict(agent.forge_users))
        if agent_name in cwd_agents:
            # A project agent's provider can spend host logins and secrets,
            # and its prompt directs whatever the user's bottle allows.
            trust_check_agent(
                agent_name, agent_path, agent.agent_provider,
                home_agent=self.host_agent(agent_name),
                bottle_name="+".join(bottle_names) or DEFAULT_BOTTLE,
                bottle=raw_bottle, sources=bottle_sources,
                source_root=self.cwd_md.parent if self.cwd_md else None,
            )

        return _manifest_with_merged_git_user(agent, raw_bottle)

    def has_agent(self, name: str) -> bool:
        return name in self.agents

    def require_agent(self, name: str) -> None:
        """Whether `name` is a discoverable agent, without parsing it."""
        if self.has_agent(name):
            return
        if self.home_md is not None and name in self.agent_files():
            return  # lazy mode: the file exists
        available = ", ".join(self.all_agent_names) or "(none)"
        raise ManifestError(
            f"agent '{name}' not defined. Available: {available}"
        )
