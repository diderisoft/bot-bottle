"""Edits a proposal makes to bottles other than this run's (PRD 0118).

The architect may propose changing a host bottle, a project bottle (`./main`)
or making a new host bottle (`new:<name>`), as typed additions: routes,
repos (with their forges' descriptions), env names and credentials. Nothing
is written until the user accepts an edit on the review; then `accept`
writes it as the approvals' "keep" does: a project bottle in the repo,
trusted on this host (the change shows in `git status`), a host bottle as
a new version.

- `target`: the file an edit writes, or EditError (a built-in bottle, a
  host bottle that doesn't exist, a new bottle whose name is taken).
- `validate_edits`: each edit checked by loading the bottle as if it were
  accepted; a grant on a known forge this host hasn't connected is kept
  unchecked (it loads once connected), as additions' are.
- `overlay`: the bottle dirs with every pending edit applied, in a temp
  dir, so validation and the review see the stack as if they were accepted.
"""

from __future__ import annotations

import contextlib
import difflib
import re
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..contract.yaml_subset import YamlSubsetError
from ..manifest.loader import (
    BUILTIN_BOTTLES_DIR, PROJECT_BOTTLE, PROJECT_PREFIX, find_bottle, load_bottle_chain,
)
from ..manifest.schema import DEFAULT_BOTTLE
from ..manifest.util import ManifestError
from .forges import Described, connected_hosts, grant_forge
from .proposal import OPTION_STACK, Proposal, ProposalEdit

NEW_PREFIX = "new:"
Dirs = list[tuple[Path, bool]]

# An env var's value is visible to the agent, so a secret never goes in one.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|PRIVATE_?KEY|CREDENTIAL|AUTH",
                         re.IGNORECASE)


class EditError(ValueError):
    """Why an edit can't be made; the message is shown."""


@dataclass(frozen=True)
class Target:
    """The file an edit writes: `path`, whether it's a project bottle, and
    whether it exists yet (a new bottle, or a project's first `./main`)."""

    path: Path
    project: bool
    exists: bool

    @property
    def name(self) -> str:
        return (PROJECT_PREFIX if self.project else "") + self.path.stem


def new_name(edit: ProposalEdit) -> str:
    """The bottle name `edit` makes, or "" when it edits an existing one."""
    return edit.bottle[len(NEW_PREFIX):].strip() if edit.bottle.startswith(NEW_PREFIX) else ""


def stack_name(name: str) -> str:
    """How a launch stack names a bottle an edit makes (`new:x` is `x`)."""
    return name[len(NEW_PREFIX):].strip() if name.startswith(NEW_PREFIX) else name


def _host_dir(dirs: Dirs) -> Path | None:
    return next((d for d, project in dirs if not project and d != BUILTIN_BOTTLES_DIR), None)


def _project_dir(dirs: Dirs) -> Path | None:
    return next((d for d, project in dirs if project), None)


def target(edit: ProposalEdit, dirs: Dirs) -> Target:
    """The file `edit` writes. EditError when it can't be edited."""
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.schema import is_valid_entity_name
    made = new_name(edit)
    if made:
        host = _host_dir(dirs)
        if not is_valid_entity_name(made):
            raise EditError(f"{made!r} isn't a bottle name (lowercase letters, digits, hyphens)")
        if host is None:
            raise EditError("there's no host bottles directory to make it in")
        if find_bottle(made, dirs) is not None:
            raise EditError(f"a bottle named {made!r} already exists")
        return Target(host / f"{made}.md", False, False)
    found = find_bottle(edit.bottle, dirs)
    if found is None:
        project = _project_dir(dirs)
        if edit.bottle == PROJECT_PREFIX + PROJECT_BOTTLE and project is not None:
            return Target(project / f"{PROJECT_BOTTLE}.md", True, False)
        raise EditError(f"there's no bottle {edit.bottle!r} to edit")
    path, project = found
    if BUILTIN_BOTTLES_DIR in path.parents or path.parent == BUILTIN_BOTTLES_DIR:
        raise EditError(f"{edit.bottle} is built in; edit a host bottle that extends it")
    return Target(path, project, True)


def _added_frontmatter(edit: ProposalEdit, forges: Described) -> dict[str, Any]:
    """What `edit` adds, as bottle frontmatter (`proposal_to_frontmatter`)."""
    # pylint: disable-next=import-outside-toplevel
    from .validate import proposal_to_frontmatter
    as_proposal = Proposal(option=OPTION_STACK, bottles=[], new_bottle=None, additions=edit.add,
                           similar=[], notes=[], forges={**forges, **edit.forges})
    return proposal_to_frontmatter(as_proposal)


def edited_frontmatter(fm: dict[str, Any], edit: ProposalEdit,
                       forges: Described) -> dict[str, Any]:
    """`fm` with `edit`'s additions merged in: a route for a host it has
    gains the new matches (`approvals.merge_route`), a repo replaces one of
    the same name, an env var it has stays as it is."""
    # pylint: disable-next=import-outside-toplevel
    from ..approvals import merge_route, with_grant
    out = dict(fm)
    added = _added_frontmatter(edit, forges)
    routes = (added.get("egress") or {}).get("routes") or []
    if routes:
        old = out.get("egress")
        egress: dict[str, Any] = dict(old) if isinstance(old, dict) else {}
        kept = [r for r in egress.get("routes") or [] if isinstance(r, dict)]
        for route in routes:
            at = next((i for i, r in enumerate(kept) if r.get("host") == route["host"]), None)
            if at is None:
                kept.append(route)
            elif kept[at].get("blocked"):
                kept[at] = route
            else:
                kept[at] = merge_route(kept[at], route)
        egress["routes"] = kept
        out["egress"] = egress
    git = added.get("git-gate") or {}
    described = git.get("forges") or {}
    for grant in git.get("repos") or []:
        out = with_grant(out, grant, forge=described.get(grant_forge(grant)))
    for var, value in (added.get("env") or {}).items():
        had = out.get("env")
        env: dict[str, Any] = dict(had) if isinstance(had, dict) else {}
        env.setdefault(var, value)
        out["env"] = env
    return out


def _read(where: Target, edit: ProposalEdit) -> tuple[dict[str, Any], str]:
    # pylint: disable-next=import-outside-toplevel
    from ..approvals import read_bottle
    if where.exists or where.project:
        fm, body = read_bottle(where.path)
        return dict(fm), body
    return {"extends": edit.extends or DEFAULT_BOTTLE}, edit.why or "Made by the bottle architect."


def edited_text(edit: ProposalEdit, where: Target, forges: Described) -> str:
    """The file `where` holds once `edit` is accepted."""
    # pylint: disable-next=import-outside-toplevel
    from ..approvals import bottle_text
    fm, body = _read(where, edit)
    return bottle_text(edited_frontmatter(fm, edit, forges), body)


def diff(edit: ProposalEdit, dirs: Dirs, forges: Described) -> list[str]:
    """`edit` as a unified diff of its bottle's file; one line saying why
    when it can't be made."""
    try:
        where = target(edit, dirs)
        before = where.path.read_text(encoding="utf-8") if where.exists else ""
        after = edited_text(edit, where, forges)
    except (EditError, OSError, YamlSubsetError) as e:
        return [f"can't edit {edit.bottle}: {e}"]
    return [line.rstrip("\n") for line in difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=where.name if where.exists else "/dev/null", tofile=where.name, n=2)]


def _unconnected_out(edit: ProposalEdit) -> ProposalEdit:
    """`edit` without its grants on forges this host hasn't connected: they
    don't load until it's connected."""
    connected = connected_hosts()
    repos = [r for r in edit.add.repos if grant_forge(r.grant) in connected]
    return replace(edit, add=replace(edit.add, repos=repos))


def unconnected(edit: ProposalEdit) -> list[str]:
    """The forges `edit`'s grants are on that this host hasn't connected."""
    connected = connected_hosts()
    return sorted({grant_forge(r.grant) for r in edit.add.repos} - connected - {""})


@contextlib.contextmanager
def overlay(edits: list[ProposalEdit], dirs: Dirs, forges: Described, *,
            loadable: bool = False) -> Generator[Dirs]:
    """`dirs` with each of `edits` applied, as if accepted: the edited files
    in a temp dir ahead of them. `loadable` leaves out grants on forges this
    host hasn't connected, so the stack loads. An edit that can't be made is
    left out (validation said why)."""
    if not edits:
        yield dirs
        return
    with tempfile.TemporaryDirectory() as tmp:
        host, project = Path(tmp) / "host", Path(tmp) / "project"
        host.mkdir()
        project.mkdir()
        for edit in edits:
            try:
                where = target(edit, dirs)
                text = edited_text(_unconnected_out(edit) if loadable else edit, where, forges)
            except (EditError, OSError, YamlSubsetError, ManifestError):
                continue
            (project if where.project else host).joinpath(where.path.name).write_text(
                text, encoding="utf-8")
        yield [(host, False), (project, True), *dirs]


def validate_edits(edits: list[ProposalEdit], dirs: Dirs,
                   known: Described) -> tuple[list[ProposalEdit], list[str]]:
    """The edits that can be made, and a note for each dropped (or part of
    one dropped). A grant on a forge nothing describes is dropped; one on a
    known forge this host hasn't connected is kept, its description added
    to the edit's `forges` so the file says where it goes."""
    kept: list[ProposalEdit] = []
    notes: list[str] = []
    connected = connected_hosts()
    for edit in edits:
        try:
            where = target(edit, dirs)
        except EditError as e:
            notes.append(f"dropped the edit of {edit.bottle}: {e}")
            continue
        forges = dict(edit.forges)
        repos = []
        for r in edit.add.repos:
            host = grant_forge(r.grant)
            if host in connected:
                repos.append(r)
            elif host in known or host in forges:
                forges.setdefault(host, dict(known.get(host, {})))
                repos.append(r)
            else:
                notes.append(f"dropped {r.grant!r} from the edit of {edit.bottle}: "
                             f"no bottle describes the forge {host or r.grant!r}")
        env = []
        for v in edit.add.env:
            if SECRET_NAME.search(v.name):
                notes.append(f"dropped env {v.name!r} from the edit of {edit.bottle}: a secret "
                             f"mustn't be an env var; propose it as a credential")
            else:
                env.append(v)
        routes = {e.host for e in edit.add.egress}
        creds = [c for c in edit.add.credentials if c.route in routes]
        checked = replace(edit, forges=forges,
                          add=replace(edit.add, repos=repos, env=env, credentials=creds))
        if checked.add.is_empty():
            notes.append(f"dropped the edit of {edit.bottle}: nothing in it was kept")
            continue
        err = _load_error(checked, where, dirs, forges)
        if err:
            notes.append(f"dropped the edit of {edit.bottle}: {err}")
            continue
        kept.append(checked)
    return kept, notes


def _load_error(edit: ProposalEdit, where: Target, dirs: Dirs, forges: Described) -> str:
    """Why the edited bottle wouldn't load, or ""."""
    try:
        text = edited_text(_unconnected_out(edit), where, forges)
    except (OSError, YamlSubsetError, ManifestError) as e:
        return str(e)
    with tempfile.TemporaryDirectory() as tmp:
        here = Path(tmp)
        (here / where.path.name).write_text(text, encoding="utf-8")
        try:
            load_bottle_chain(where.name, [(here, where.project), *dirs])
        except ManifestError as e:
            return str(e).split("\n", 1)[0]
    return ""


def accept(edit: ProposalEdit, dirs: Dirs, forges: Described) -> Target:
    """Write `edit`: a project bottle trusted on this host (for the user to
    commit), a host bottle as a new version. EditError when it can't be."""
    # pylint: disable=import-outside-toplevel
    from ..approvals import trust_project_bottle
    from ..manifest.versions import record_or_warn
    pending = unconnected(edit)
    if pending:
        raise EditError(f"connect {', '.join(pending)} first (c)")
    where = target(edit, dirs)
    text = edited_text(edit, where, forges)
    where.path.parent.mkdir(parents=True, exist_ok=True)
    where.path.write_text(text, encoding="utf-8")
    if where.project:
        trust_project_bottle(where.path.parent.parent.parent, where.path.stem)
    else:
        record_or_warn(f"architect: {'make' if not where.exists else 'edit'} {where.name}",
                       root=where.path.parent.parent)
    return replace(where, exists=True)


def _repo_name(grant: str) -> str:
    """`owner/repo` of a grant (its alias's grant), else the grant as given."""
    bare = grant.split("=", 1)[1] if "=" in grant.split("/", 1)[0] else grant
    parts = bare.removesuffix(":write").split("/")
    return "/".join(parts[1:]) if len(parts) == 3 else grant


def summary(edit: ProposalEdit) -> str:
    """What `edit` adds, in few words: `+ api.linear.app · + acme/app (push)`."""
    add = edit.add
    parts = [f"+ {e.host}" for e in add.egress]
    parts += [f"+ {_repo_name(r.grant)}" + (" (push)" if r.grant.endswith(":write") else "")
              for r in add.repos]
    parts += [f"+ ${c.name}" for c in add.credentials]
    parts += [f"+ {v.name}" for v in add.env]
    return " · ".join(parts) or "nothing"


__all__ = ["NEW_PREFIX", "EditError", "Target", "accept", "diff", "edited_frontmatter",
           "edited_text", "new_name", "overlay", "stack_name", "summary", "target",
           "unconnected", "validate_edits"]
