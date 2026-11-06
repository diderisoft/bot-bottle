"""`bot-bottle bottles migrate <name>`: rewrite a bottle in the old git-gate
format (`manifest/legacy_git_gate.py`) as grants on forges this host connected.

The file is edited as text, so comments and everything outside the repos it
converts stay as they were: the old repos' entries become grants. The result
is checked by parsing it back before it's written. The forge must already be
connected; connecting needs a token typed by the operator, so `migrate` never
does it.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
from .forges import FORGE_KINDS
from .legacy_git_gate import Plan, connected_homes, explain, list_form, old_repos, plan
from .loader import find_bottle, load_bottle_chain
from .util import ManifestError, as_json_object
from .versions import record, record_hand_edits


class MigrateError(ManifestError):
    """The bottle can't be migrated; the message says what to do instead."""


@dataclass(frozen=True)
class Migration:
    """A bottle's file before and after, and what to tell the operator."""

    bottle: str
    path: Path
    project: bool
    before: str
    after: str
    plan: Plan
    notes: tuple[str, ...]

    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.before.splitlines(keepends=True), self.after.splitlines(keepends=True),
            fromfile=f"{self.path} (now)", tofile=f"{self.path} (migrated)"))


def prepare(name: str, dirs: Sequence[tuple[Path, bool]]) -> Migration:
    """What migrating `name` would write. Raises MigrateError when it's
    already migrated, or can't be yet (its forge isn't connected), saying why."""
    found = find_bottle(name, dirs)
    if found is None:
        raise MigrateError(f"bottle '{name}' not found")
    path, project = found
    text = path.read_text()
    try:
        fm, _ = parse_frontmatter(text)
    except YamlSubsetError as e:
        raise MigrateError(f"{path}: {e}") from e
    git = fm.get("git-gate")
    repos = git.get("repos") if isinstance(git, dict) else None
    old = old_repos(repos, FORGE_KINDS) if isinstance(repos, dict) else []
    if not isinstance(repos, dict) or not old:
        raise MigrateError(f"bottle '{name}' has no repos in the old git-gate format; "
                           f"nothing to migrate")
    p = plan(name, old, connected_homes())
    if p.missing or p.unconvertible:
        why = (f"the forge {', '.join(p.missing)} isn't connected on this host yet; "
               f"connect it first" if p.missing else "some repos can't be converted")
        raise MigrateError(f"{why}.\n{explain(p, repos)}")
    after = rewrite(text, p, repos)
    return Migration(bottle=name, path=path, project=project, before=text, after=after,
                     plan=p, notes=tuple(_notes(p)))


def _notes(p: Plan) -> list[str]:
    notes: list[str] = []
    for host, home in p.homes.items():
        repos = [r for r in p.repos if r.forge_host == host]
        envs = sorted({r.token_env for r in repos if r.token_env})
        if envs and home.token_secrets and not set(envs) <= set(home.token_secrets):
            notes.append(f"{host}: keys will be minted with the connected user's token "
                         f"(secret {home.token_secrets[0]}), no longer {', '.join(envs)}")
        if not home.host_key:
            notes.append(f"{host}: the connection pins no host key, and the repos' own "
                         f"host_key lines go; connect it again to pin one")
        elif any(r.host_key and r.host_key != home.host_key for r in repos):
            notes.append(f"{host}: the repos' host_key differs from the one the connection "
                         f"pins; the connection's is used")
        ssh = {r.ssh for r in repos} - {home.ssh}
        if home.ssh and ssh:
            notes.append(f"{host}: upstreams move from {', '.join(sorted(ssh))} to the "
                         f"connection's SSH address {home.ssh}")
    return notes


def apply(m: Migration, dirs: Sequence[tuple[Path, bool]], *,
          root: Path | None = None) -> str:
    """Write `m`. A host bottle's write is a version (any hand edit before it is
    versioned first, so `bottles revert` can undo the migration); returns it,
    or "" for a project bottle, left for the project to commit. Restores the
    file and raises MigrateError if the result doesn't load."""
    if not m.project:
        record_hand_edits(root=root)
    m.path.write_text(m.after)
    try:
        load_bottle_chain(m.bottle, dirs)
    except ManifestError as e:
        m.path.write_text(m.before)
        raise MigrateError(f"the migrated bottle doesn't load, so it was left as it "
                           f"was: {e}") from e
    if m.project:
        return ""
    return record(f"bottles migrate: {m.bottle} to forge grants", root=root)


# --- Text rewriting -------------------------------------------------------

_KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>\"[^\"]*\"|'[^']*'|[^\s:#'\"][^:#]*?)\s*:"
                     r"(?:\s+(?P<value>.*?))?\s*$")


def _content(line: str) -> bool:
    """Whether a line is YAML content (not blank, not a comment)."""
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _key(line: str) -> tuple[int, str, str] | None:
    m = _KEY_RE.match(line)
    if not m:
        return None
    key = m.group("key")
    if key[:1] in "\"'":
        key = key[1:-1]
    return len(m.group("indent")), key, m.group("value") or ""


def _block_end(lines: list[str], start: int, indent: int) -> int:
    """The index after the block opened at `start` (lines indented deeper than
    `indent`), not counting trailing blank or comment lines."""
    end = start + 1
    last = start
    while end < len(lines):
        if _content(lines[end]):
            if _indent(lines[end]) <= indent:
                break
            last = end
        end += 1
    return last + 1


def _find_key(lines: list[str], lo: int, hi: int, indent: int, key: str) -> int:
    for i in range(lo, hi):
        if _content(lines[i]) and _indent(lines[i]) == indent:
            k = _key(lines[i])
            if k and k[1] == key:
                return i
    return -1


def _child_indent(lines: list[str], lo: int, hi: int) -> int:
    return next((_indent(lines[i]) for i in range(lo, hi) if _content(lines[i])), -1)


def _rewrite_repos(fm: list[str], p: Plan, repos: Mapping[str, object]) -> list[str]:
    git = _find_key(fm, 0, len(fm), 0, "git-gate")
    if git < 0:
        raise MigrateError("no top-level git-gate: block")
    git_end = _block_end(fm, git, 0)
    gi = _child_indent(fm, git + 1, git_end)
    at = _find_key(fm, git + 1, git_end, gi, "repos")
    k = _key(fm[at]) if at >= 0 else None
    if k is None or k[2]:
        raise MigrateError("git-gate.repos isn't a block mapping")
    end = _block_end(fm, at, gi)
    ei = _child_indent(fm, at + 1, end)
    if list_form(p, repos):
        return fm[:at + 1] + [f"{' ' * ei}- {g}" for g in p.grants.values()] + fm[end:]
    out = fm[:at + 1]
    i = at + 1
    while i < end:
        entry = _key(fm[i]) if _content(fm[i]) and _indent(fm[i]) == ei else None
        if entry is None or entry[1] not in p.grants:
            out.append(fm[i])
            i += 1
            continue
        out.append(f"{' ' * ei}{fm[i].strip().split(':', 1)[0]}: {p.grants[entry[1]]}")
        i = _block_end(fm, i, ei)
    return out + fm[end:]


def rewrite(text: str, p: Plan, repos: Mapping[str, object]) -> str:
    """`text` (a bottle file) with `p` applied, everything else as it was.
    Raises MigrateError when the result wouldn't read back as intended."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise MigrateError("the file doesn't start with --- frontmatter")
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    if close < 0:
        raise MigrateError("the frontmatter has no closing ---")
    fm_before, _ = parse_frontmatter(text)
    fm = _rewrite_repos(lines[1:close], p, repos)
    out = "\n".join([lines[0], *fm, *lines[close:]])
    _check(fm_before, out, p, repos)
    return out


def _check(before: Mapping[str, object], after_text: str, p: Plan,
           repos: Mapping[str, object]) -> None:
    """The rewritten file must parse to the old frontmatter with only the
    repos changed."""
    expected = dict(before)
    git = dict(as_json_object(before.get("git-gate"), "git-gate"))
    git["repos"] = (list(p.grants.values()) if list_form(p, repos)
                    else {n: p.grants.get(n, e) for n, e in repos.items()})
    expected["git-gate"] = git
    try:
        after, _ = parse_frontmatter(after_text)
    except YamlSubsetError as e:
        raise MigrateError(f"couldn't rewrite it safely ({e}); edit it by hand") from e
    if dict(after) != expected:
        raise MigrateError("couldn't rewrite it safely (its layout is unusual); "
                           "edit it by hand")


__all__ = ["MigrateError", "Migration", "apply", "prepare", "rewrite"]
