"""Whether a library file parses as the bottle or agent its path says it is
(PRD 0131): what a host checks before it syncs a hand edit, so a half-written
file never reaches every host.

Only the file itself is read: its frontmatter, keys and values, as the
loaders read them. What depends on this host (an `extends:` parent, a forge's
connection, trust) is a launch's to check, so a file that's fine here is fine
on every host.
"""

from __future__ import annotations

from pathlib import Path

from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
from .agent import agent_from_frontmatter
from .bottle import ManifestBottle
from .forges import (check_grant_paths, credentials_raw, forges_raw, granted_forges,
                     normalize_repos, refuse_long_form, strip_chain_keys)
from .schema import (entity_name_from_path, validate_agent_frontmatter_keys,
                     validate_bottle_frontmatter_keys)
from .util import ManifestError

KINDS = ("agents", "bottles")


def checked(rel: str) -> bool:
    """Whether `rel` (relative to the library's root) is a file this checks:
    a `*.md` directly under `agents/` or `bottles/`."""
    path = Path(rel)
    return path.suffix == ".md" and len(path.parts) == 2 and path.parts[0] in KINDS


def names(files: tuple[str, ...] | list[str]) -> str:
    """`bottles/dev.md` as `dev`, `agents/x.md` as `agent x`, others (such as
    `bottles/sub/x.md`) as is, in a list a sentence can hold."""
    shown = [(f"agent {f[7:-3]}" if f.startswith("agents/") else f[8:-3]) if checked(f) else f
             for f in files]
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


def problem(rel: str, text: str) -> str:
    """Why `text`, the content of `rel`, doesn't parse as its kind; "" when it
    does."""
    path = Path(rel)
    name = entity_name_from_path(path)
    if name is None:
        return f"the filename must match [a-z][a-z0-9-]*.md (got {path.name!r})"
    try:
        fm, body = parse_frontmatter(text)
        if path.parts[0] == "agents":
            validate_agent_frontmatter_keys(path, fm.keys())
            agent_from_frontmatter(name, dict(fm), body)
        else:
            _bottle(name, path, dict(fm))
    except (YamlSubsetError, ManifestError) as e:
        return str(e)
    return ""


def _bottle(name: str, path: Path, raw: dict[str, object]) -> None:
    validate_bottle_frontmatter_keys(path, raw.keys())
    parents = raw.pop("extends", None)
    if parents is not None and not (isinstance(parents, str) or (
            isinstance(parents, list) and all(isinstance(p, str) for p in parents))):
        raise ManifestError(f"bottle '{name}' extends: a bottle's name, or a list of them")
    credentials_raw(name, raw)
    forges = forges_raw(name, raw)
    granted_forges(name, raw)
    # Grants expand through this host's forge connections, so the parser
    # reads the rest without them.
    raw = strip_chain_keys(normalize_repos(name, raw))
    git = raw.get("git_gate")
    if isinstance(git, dict) and isinstance(git.get("repos"), dict):
        # Every repo is a grant (ADR 0023), as a launch says; planned without
        # this host's connections, so the answer is the same everywhere.
        refuse_long_form(name, git["repos"], homes={})
        # A grant on a forge this file describes fits its kind; one it
        # doesn't describe is a parent's or the connection's to say.
        check_grant_paths(name, raw, forges)
        raw["git_gate"] = {**git, "repos": {}}
    ManifestBottle.from_dict(name, raw)


__all__ = ["checked", "names", "problem"]
