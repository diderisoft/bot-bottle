"""Make a proposal runnable on this host before anyone is asked to run it
(PRD 0117).

Two things a model can't see from bottle names alone:

- **A repo grant needs its forge.** A grant `<forge>/<owner>/<repo>` loads
  only in a stack where some bottle declares `<forge>` (`git-gate.forges`).
  When the proposal's bottles don't, the library bottle that does is stacked
  for it: the most basic declarer, the one other declarers extend (`gitea`,
  which `forge connect` writes, rather than a bottle built on it). With no
  declarer, or several unrelated ones, the grant is left for validation to
  drop, and the note names the choices.
- **A bottle's secrets must exist here.** An egress route that injects a
  credential, or a granted repo's forge token, names a secret the launch
  resolves from the host env or the secrets file. A proposed bottle whose
  secret is missing is dropped (the stack falls back to `basic` when it was
  the only one); a secret the proposal can't do without makes it not
  runnable, with `Proposal.blocked` saying which. Secret values are never
  read into the proposal, only whether a name resolves.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..errors import SecretsFileError
from ..manifest.bottle import ManifestBottle
from ..manifest.forges import parse_grant, split_grant_alias
from ..manifest.loader import load_bottle_chain, scan_bottle_names
from ..manifest.schema import DEFAULT_BOTTLE
from ..manifest.util import ManifestError
from ..paths import host_secrets_path
from ..secret_source import ChainSecrets, FileSecrets, MappingSecrets, SecretSource
from .proposal import OPTION_NEW, OPTION_NEW_EXTENDING, Proposal

Dirs = list[tuple[Path, bool]]


def host_secrets(env: dict[str, str] | None = None, path: Path | None = None) -> SecretSource:
    """Where a launch on this host finds secrets: the environment, then the
    host secrets file."""
    return ChainSecrets(MappingSecrets(dict(os.environ if env is None else env)),
                        FileSecrets(path or host_secrets_path()))


def has_secret(source: SecretSource, name: str) -> bool:
    """Whether `name` resolves to a non-empty value. An unreadable secrets
    file counts as not having it."""
    try:
        return bool(source.get(name))
    except SecretsFileError:
        return False


def library(dirs: Dirs) -> list[str]:
    """Every bottle name the stack can use (`./name` for a project's)."""
    names: list[str] = []
    for directory, project in dirs:
        for name in scan_bottle_names(directory):
            ref = f"./{name}" if project else name
            if ref not in names:
                names.append(ref)
    return names


def _load(name: str, dirs: Dirs) -> tuple[ManifestBottle, tuple[str, ...]] | None:
    """The resolved bottle and the names in its chain, or None if it doesn't
    load."""
    try:
        bottle, sources = load_bottle_chain(name, dirs)
    except ManifestError:
        return None
    return bottle, tuple(s.name for s in sources)


def forges_of(bottle: ManifestBottle) -> dict[str, str]:
    """The forges a bottle's chain declares, name to token secret."""
    return {f.name: f.token_secret for f in bottle.forges}


def needed_secrets(bottle: ManifestBottle) -> list[str]:
    """Secrets a launch of `bottle` resolves: each injected egress
    credential and each granted repo's forge token."""
    names = [r.TokenRef for r in bottle.egress.routes if r.TokenRef]
    names += [e.Key.forge_token_env for e in bottle.git if e.Key.forge_token_env]
    return list(dict.fromkeys(names))


def missing_secrets(name: str, dirs: Dirs, source: SecretSource) -> list[str]:
    """`name`'s secrets that don't resolve on this host (names only)."""
    loaded = _load(name, dirs)
    if loaded is None:
        return []
    return [s for s in needed_secrets(loaded[0]) if not has_secret(source, s)]


def library_facts(name: str, dirs: Dirs, source: SecretSource) -> tuple[list[str], list[str]]:
    """For the architect's library: the forges `name` declares and its
    secrets that don't resolve here (names only)."""
    loaded = _load(name, dirs)
    if loaded is None:
        return [], []
    bottle = loaded[0]
    return (sorted(forges_of(bottle)),
            [s for s in needed_secrets(bottle) if not has_secret(source, s)])


def forge_declarers(forge: str, dirs: Dirs) -> list[str]:
    """The most basic library bottles declaring `forge`: a declarer is left
    out when it extends another declarer."""
    chains = {}
    for name in library(dirs):
        loaded = _load(name, dirs)
        if loaded is not None and forge in forges_of(loaded[0]):
            chains[name] = loaded[1]
    return [n for n, chain in chains.items()
            if not any(other != n and other in chain for other in chains)]


def _grant_forge(grant: str) -> str:
    try:
        return parse_grant(split_grant_alias(grant)[1], "proposal")[0]
    except (ManifestError, ValueError):
        return ""


def _stack_forges(proposal: Proposal, dirs: Dirs) -> None:
    stacked = proposal.option not in (OPTION_NEW_EXTENDING, OPTION_NEW)
    declared: set[str] = set()
    for name in _base(proposal):
        loaded = _load(name, dirs)
        if loaded is not None:
            declared |= forges_of(loaded[0]).keys()
    for repo in proposal.additions.repos:
        forge = _grant_forge(repo.grant)
        if not forge or forge in declared:
            continue
        found = forge_declarers(forge, dirs)
        if len(found) == 1 and stacked:
            proposal.bottles.append(found[0])
            declared |= {forge}
            proposal.notes.append(f"stacked {found[0]}: it declares the forge {forge} "
                                  f"for the repo grant")
        elif len(found) > 1:
            proposal.notes.append(f"several bottles declare the forge {forge} "
                                  f"({', '.join(sorted(found))}); stack one to keep {repo.grant}")


def _base(proposal: Proposal) -> list[str]:
    if proposal.option in (OPTION_NEW_EXTENDING, OPTION_NEW):
        extends = proposal.new_bottle.extends if proposal.new_bottle else ""
        return [extends or DEFAULT_BOTTLE]
    return list(proposal.bottles)


def _drop_unrunnable(proposal: Proposal, dirs: Dirs, source: SecretSource) -> None:
    stacked = proposal.option not in (OPTION_NEW_EXTENDING, OPTION_NEW)
    dropped = False
    for name in _base(proposal):
        missing = missing_secrets(name, dirs, source)
        if not missing:
            continue
        unset = ", ".join(missing)
        if stacked:
            proposal.bottles.remove(name)
            dropped = True
            proposal.notes.append(f"dropped {name}: its secret {unset} isn't set on this host "
                                  f"(host env or secrets file)")
        else:
            proposal.blocked = (f"{name} needs {unset}, which isn't set on this host "
                                f"(host env or secrets file)")
    # What's left must still start from `basic` (ADR 0011): a forge's bottle,
    # say, carries only the forge.
    if dropped and not any(DEFAULT_BOTTLE in _chain(n, dirs) for n in proposal.bottles):
        proposal.bottles.insert(0, DEFAULT_BOTTLE)
    for repo in proposal.additions.repos:
        forge = _grant_forge(repo.grant)
        token = next((forges_of(b)[forge] for b in (_bottle(n, dirs) for n in _base(proposal))
                      if b is not None and forge in forges_of(b)), "")
        if token and not has_secret(source, token):
            proposal.blocked = (f"the repo grant {repo.grant} needs the forge token {token}, "
                                f"which isn't set on this host (host env or secrets file)")


def _bottle(name: str, dirs: Dirs) -> ManifestBottle | None:
    loaded = _load(name, dirs)
    return loaded[0] if loaded else None


def _chain(name: str, dirs: Dirs) -> tuple[str, ...]:
    loaded = _load(name, dirs)
    return loaded[1] if loaded else ()


def make_runnable(proposal: Proposal, dirs: Dirs, source: SecretSource | None = None,
                  *, secrets: Callable[[], SecretSource] = host_secrets) -> Proposal:
    """Stack what the proposal's repo grants need, drop bottles whose secrets
    aren't here, and set `blocked` when it still can't run. Mutates and
    returns `proposal`."""
    source = source or secrets()
    proposal.blocked = ""
    _stack_forges(proposal, dirs)
    _drop_unrunnable(proposal, dirs, source)
    return proposal


__all__ = ["forge_declarers", "forges_of", "has_secret", "host_secrets", "library",
           "library_facts", "make_runnable", "missing_secrets", "needed_secrets"]
