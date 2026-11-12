"""`bot-bottle bottles migrate <name>`: rewrite what a bottle still writes in
an old format, in one pass:

- repos in the old git-gate format (`manifest/legacy_git_gate.py`) become
  grants on forges this host connected (edited as text, so comments stay);
- the old egress shape (`manifest/upgrade.py`) becomes the current one, each
  top-level block that changes written afresh and the rest left as written;
- a grant whose forge nothing in the bottle's chain declares gets the forge's
  declaration (`git_gate.forges`) from this host's connection, so the bottle
  isn't partial (ADR 0021).

Other old forms are refused at load, naming their new form, for a hand edit.

The result is checked by parsing it back before it's written, and written as
a new version. Running it again finds nothing to do. The forge must already be
connected; connecting needs a token typed by the operator, so `migrate` never
does it.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
from .forges import FORGE_KINDS
from .legacy_git_gate import Plan, connected_homes, explain, list_form, old_repos, plan
from .loader import find_bottle, load_bottle_chain
from .upgrade import UpgradeError, rewrite_text, upgrade_bottle
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
    plan: Plan | None
    notes: tuple[str, ...]
    # What else changed, a line each.
    changes: tuple[str, ...] = ()

    @property
    def what(self) -> str:
        """What it migrates to, for the version's message."""
        declares = [c for c in self.changes if c.startswith("declares the forge ")]
        parts = (["forge grants"] if self.plan is not None else []) + (
            ["the current format"] if len(declares) < len(self.changes) else []) + (
            ["declared forges"] if declares else [])
        return " and ".join(parts)

    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.before.splitlines(keepends=True), self.after.splitlines(keepends=True),
            fromfile=f"{self.path} (now)", tofile=f"{self.path} (migrated)"))


def _git_key(fm: Mapping[str, object]) -> str:
    return "git-gate" if "git-gate" in fm else "git_gate"


def prepare(name: str, dirs: Sequence[tuple[Path, bool]]) -> Migration:
    """What migrating bottle `name` would write. Raises MigrateError when it's
    already migrated, or can't be yet (its forge isn't connected), saying why."""
    found = find_bottle(name, dirs)
    if found is None:
        raise MigrateError(f"bottle '{name}' not found")
    path, project = found
    text = _read(path)
    fm = _frontmatter(path, text)
    git = fm.get(_git_key(fm))
    repos = git.get("repos") if isinstance(git, dict) else None
    old = old_repos(repos, FORGE_KINDS) if isinstance(repos, dict) else []
    after = text
    p: Plan | None = None
    if isinstance(repos, dict) and old:
        p = plan(name, old, connected_homes())
        if p.missing or p.unconvertible:
            why = (f"the forge {', '.join(p.missing)} isn't connected on this host yet; "
                   f"connect it first" if p.missing else "some repos can't be converted")
            raise MigrateError(f"{why}.\n{explain(p, repos)}")
        after = rewrite(after, p, repos)
    try:
        doc, changes, edits = upgrade_bottle(_frontmatter(path, after))
        declared = declare_forges(name, doc, dirs)
        if declared:
            changes += [f"declares the forge {host} (from this host's connection)"
                        for host in declared]
            edits.append(add_forges(declared))
        if changes:
            after = rewrite_text(after, doc, edits)
    except UpgradeError as e:
        raise MigrateError(f"bottle '{name}': {e}") from e
    if p is None and not changes:
        raise MigrateError(f"bottle '{name}' is already in the current format; "
                           f"nothing to migrate")
    return Migration(bottle=name, path=path, project=project, before=text, after=after,
                     plan=p, notes=tuple(_notes(p)) if p else (), changes=tuple(changes))


def _declared_by_parents(doc: Mapping[str, object],
                         dirs: Sequence[tuple[Path, bool]]) -> frozenset[str]:
    parents = doc.get("extends")
    names = [parents] if isinstance(parents, str) else parents if isinstance(parents, list) else []
    declared: frozenset[str] = frozenset()
    for parent in names:
        if not isinstance(parent, str):
            continue
        try:
            declared |= load_bottle_chain(parent, dirs)[0].declared_forges
        except ManifestError:
            continue  # it says so itself when it's loaded; declaring here is harmless
    return declared


def declare_forges(name: str, doc: dict[str, object],
                   dirs: Sequence[tuple[Path, bool]]) -> dict[str, dict[str, object]]:
    """Add to `doc` (a bottle's current-format frontmatter) a declaration for
    each forge its chain's grants use (its own, or a parent's) that nothing in
    the chain declares, from this host's connection; the declarations added,
    by host. A declaration here covers the whole chain. MigrateError when
    such a forge isn't connected here: migrate never invents a forge."""
    # pylint: disable=import-outside-toplevel
    from .. import forge_connections
    from .forges import described_connection, granted_forges
    found = doc.get("git_gate")
    git: dict[str, object] = found if isinstance(found, dict) else {}
    own = git.get("forges")
    declared = set(own) if isinstance(own, dict) else set()
    try:  # a parent's grants too; the bottle on disk may not load yet (old format)
        inherited = load_bottle_chain(name, dirs)[0].undeclared_forges
    except ManifestError:
        inherited = frozenset()
    missing = sorted((granted_forges(name, doc) | inherited)
                     - declared - _declared_by_parents(doc, dirs))
    if not missing:
        return {}
    added: dict[str, object] = dict(own) if isinstance(own, dict) else {}
    new: dict[str, dict[str, object]] = {}
    for host in missing:
        conn = forge_connections.get(host)
        if conn is None:
            raise MigrateError(f"bottle '{name}' (or a bottle it extends) grants repos on "
                               f"{host}, which nothing declares and this host hasn't "
                               f"connected; connect it first "
                               f"(`bot-bottle forge connect https://{host}`), or declare it "
                               f"under git_gate.forges by hand")
        added[host] = new[host] = described_connection(conn)
    git["forges"] = added
    doc["git_gate"] = git
    return new


def add_forges(declared: Mapping[str, Mapping[str, object]]) -> "Callable[[list[str]], list[str]]":
    """A line edit adding `declared` under `git_gate.forges`, as lines in the
    block, so the rest of it (comments included) stays as written. A
    `forges` written inline (`forges: {...}`) is left to the block rewrite."""
    def entries(indent: int) -> list[str]:
        out: list[str] = []
        for host, d in declared.items():
            out.append(" " * indent + f"{host}:")
            out += [" " * (indent + 2) + f"{k}: {v}" for k, v in d.items()]
        return out

    def edit(lines: list[str]) -> list[str]:
        out = list(lines)
        top = next((i for i, line in enumerate(out) if _content(line) and _indent(line) == 0
                    and (_key(line) or (0, "", ""))[1] in ("git_gate", "git-gate")), -1)
        if top < 0:
            return [*out, "git_gate:", "  forges:", *entries(4)]
        if (_key(out[top]) or (0, "", ""))[2]:
            return out  # written inline: the block's rewrite says it
        end = _block_end(out, top, 0)
        child = _child_indent(out, top + 1, end)
        child = child if child > 0 else 2
        at = _find_key(out, top + 1, end, child, "forges")
        if at < 0:
            out[end:end] = [" " * child + "forges:", *entries(child + 2)]
            return out
        if (_key(out[at]) or (0, "", ""))[2]:
            return out  # inline: the block's rewrite says it
        sub_end = _block_end(out, at, child)
        sub = _child_indent(out, at + 1, sub_end)
        out[sub_end:sub_end] = entries(sub if sub > child else child + 2)
        return out
    return edit


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as e:
        raise MigrateError(f"{path}: {e}") from e


def _frontmatter(path: Path, text: str) -> dict[str, object]:
    try:
        return dict(parse_frontmatter(text)[0])
    except YamlSubsetError as e:
        raise MigrateError(f"{path}: {e}") from e


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
    return record(f"bottles migrate: {m.bottle} to {m.what}", root=root)


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
        git = _find_key(fm, 0, len(fm), 0, "git_gate")
    if git < 0:
        raise MigrateError("no top-level git_gate: block")
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
    key = _git_key(before)
    git = dict(as_json_object(before.get(key), key))
    git["repos"] = (list(p.grants.values()) if list_form(p, repos)
                    else {n: p.grants.get(n, e) for n, e in repos.items()})
    expected[key] = git
    try:
        after, _ = parse_frontmatter(after_text)
    except YamlSubsetError as e:
        raise MigrateError(f"couldn't rewrite it safely ({e}); edit it by hand") from e
    if dict(after) != expected:
        raise MigrateError("couldn't rewrite it safely (its layout is unusual); "
                           "edit it by hand")


__all__ = ["MigrateError", "Migration", "apply", "prepare", "rewrite"]
