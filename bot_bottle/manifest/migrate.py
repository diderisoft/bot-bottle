"""`bot-bottle bottles migrate <name>`: rewrite what a bottle still writes in
an old format:

- repos in the old git-gate format (`manifest/legacy_git_gate.py`) become
  grants on forges this host connected;
- a route's auth in the old format (`scheme`, `header`, `token_ref`) becomes
  `type`, `name` and `value` (`new_auth`; `manifest.egress.OldAuthFormatError`).

The file is edited as text, so comments and everything else stay as they
were. The result is checked by parsing it back before it's written. The forge
must already be connected; connecting needs a token typed by the operator, so
`migrate` never does it.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
from ..secret_ref import is_name, stored
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
    plan: Plan | None
    notes: tuple[str, ...]
    # The routes whose auth becomes `type` and `value`, by host.
    auths: tuple[str, ...] = ()

    @property
    def what(self) -> str:
        """What it migrates to, for the version's message."""
        parts = (["forge grants"] if self.plan is not None else []) + (
            ["auth type and value"] if self.auths else [])
        return " and ".join(parts)

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
    auths = old_auths(fm)
    if not (isinstance(repos, dict) and old) and not auths:
        raise MigrateError(f"bottle '{name}' has no repos in the old git-gate format and "
                           f"no route auth in the old format; nothing to migrate")
    after = text
    p: Plan | None = None
    if isinstance(repos, dict) and old:
        p = plan(name, old, connected_homes())
        if p.missing or p.unconvertible:
            why = (f"the forge {', '.join(p.missing)} isn't connected on this host yet; "
                   f"connect it first" if p.missing else "some repos can't be converted")
            raise MigrateError(f"{why}.\n{explain(p, repos)}")
        after = rewrite(after, p, repos)
    if auths:
        after = rewrite_auth(after)
    return Migration(bottle=name, path=path, project=project, before=text, after=after,
                     plan=p, notes=tuple(_notes(p)) if p else (),
                     auths=tuple(host for host, _ in auths))


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


# --- auth: scheme/header/token_ref -> type/name/value ------------------------

_OLD_AUTH_KEYS = ("scheme", "header", "token_ref")


def _routes(fm: Mapping[str, object]) -> list[object]:
    egress = fm.get("egress")
    routes = egress.get("routes") if isinstance(egress, dict) else None
    return list(routes) if isinstance(routes, list) else []


def _auth(route: object) -> dict[str, object] | None:
    inspect = route.get("inspect") if isinstance(route, dict) else None
    auth = inspect.get("auth") if isinstance(inspect, dict) else None
    return auth if isinstance(auth, dict) else None


def _host(route: object) -> str:
    host = route.get("host") if isinstance(route, dict) else None
    return str(host or "?")


def old_auths(fm: Mapping[str, object]) -> list[tuple[str, dict[str, object]]]:
    """Each route whose auth is still in the old format (`scheme`, `header`,
    `token_ref`), as (host, auth)."""
    return [(_host(r), a) for r in _routes(fm)
            if (a := _auth(r)) is not None and any(k in a for k in _OLD_AUTH_KEYS)]


def new_auth(host: str, auth: Mapping[str, object]) -> dict[str, object]:
    """An old-format auth as `type`, `name` (a header's) and `value`:
    `scheme: Bearer` is `type: bearer`, `scheme: token` is `type: token`,
    `header: X` alone is `type: header, name: X`, `token_ref: N` is `value:
    secret:N`. Raises MigrateError for what has no new form, saying what to
    write by hand."""
    where = f"egress route {host}"
    scheme, header = auth.get("scheme"), auth.get("header")
    if scheme and header:
        raise MigrateError(
            f"{where}: auth sends {header}: {scheme} <value> (header and scheme together), "
            f"which auth no longer does. Write it by hand: for Authorization, type: "
            f"{str(scheme).lower()} with the value; else type: header, name: {header}, "
            f"with the secret's value starting '{scheme} '")
    if any(k in auth for k in ("type", "name")):
        raise MigrateError(f"{where}: auth mixes the old keys with type or name; edit it "
                           f"by hand")
    out: dict[str, object] = {}
    if scheme:
        if scheme not in ("Bearer", "token"):
            raise MigrateError(f"{where}: auth.scheme {scheme!r} isn't Bearer or token; "
                               f"edit it by hand")
        out["type"] = str(scheme).lower()
    elif header:
        out["type"] = "header"
        out["name"] = header
    else:
        raise MigrateError(f"{where}: auth has no scheme or header; edit it by hand")
    ref = auth.get("token_ref")
    if ref is not None:
        if "value" in auth:
            raise MigrateError(f"{where}: auth sets both token_ref and value; edit it by hand")
        if not isinstance(ref, str) or not is_name(ref):
            raise MigrateError(f"{where}: auth.token_ref {ref!r} isn't a secret name "
                               f"(letters, digits and _); edit it by hand")
        out["value"] = stored(ref)
    elif "value" in auth:
        out["value"] = auth["value"]
    out.update({k: v for k, v in auth.items() if k not in (*_OLD_AUTH_KEYS, "value")})
    return out


_Q = r"(?P<q>[\"']?)"


def _flow_scheme(m: "re.Match[str]") -> str:
    return f"{m['pre']}type{m['sp']}{m['v'].lower()}"


def _flow_header(m: "re.Match[str]") -> str:
    return f"{m['pre']}type{m['sp']}header, name{m['sp']}{m['q']}{m['v']}{m['q']}"


def _flow_token_ref(m: "re.Match[str]") -> str:
    return f"{m['pre']}value{m['sp']}{m['q']}secret:{m['v']}{m['q']}"


_FLOW_SUBS: "tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...]" = (
    (re.compile(r"(?P<pre>[{,]\s*)scheme(?P<sp>\s*:\s*)" + _Q + r"(?P<v>Bearer|token)(?P=q)"
                r"(?=\s*[,}])"), _flow_scheme),
    (re.compile(r"(?P<pre>[{,]\s*)header(?P<sp>\s*:\s*)" + _Q + r"(?P<v>[^\s,}'\"]+)(?P=q)"
                r"(?=\s*[,}])"), _flow_header),
    (re.compile(r"(?P<pre>[{,]\s*)token_ref(?P<sp>\s*:\s*)" + _Q +
                r"(?P<v>[A-Za-z_][A-Za-z0-9_]*)(?P=q)(?=\s*[,}])"), _flow_token_ref),
)
_BLOCK_RE = re.compile(r"^(?P<ind> *)(?P<k>scheme|header|token_ref)(?P<sp>\s*:\s*)" + _Q +
                       r"(?P<v>[^\s'\"#]+)(?P=q)(?P<rest>\s*(?:#.*)?)$")


def _block_line(line: str) -> list[str]:
    m = _BLOCK_RE.match(line)
    if m is None:
        return [line]
    ind, sp, q, v, rest = m["ind"], m["sp"], m["q"], m["v"], m["rest"]
    if m["k"] == "scheme":
        return [f"{ind}type{sp}{v.lower()}{rest}"]
    if m["k"] == "header":
        return [f"{ind}type{sp}header", f"{ind}name{sp}{q}{v}{q}{rest}"]
    return [f"{ind}value{sp}{q}secret:{v}{q}{rest}"]


def _rewrite_auth_lines(fm: list[str]) -> list[str]:
    out: list[str] = []
    block = -1  # the indent of the `auth:` block being rewritten, else -1
    for line in fm:
        if block >= 0 and _content(line) and _indent(line) <= block:
            block = -1
        k = _key(line) if _content(line) else None
        if block >= 0 and _content(line):
            out += _block_line(line)
            continue
        if k is not None and k[1] == "auth":
            if not k[2]:
                block = k[0]
            elif k[2].startswith("{"):
                for pattern, repl in _FLOW_SUBS:
                    line = pattern.sub(repl, line)
        out.append(line)
    return out


def rewrite_auth(text: str) -> str:
    """`text` (a bottle file) with each route's old-format auth written as
    `type`, `name` and `value` (`new_auth`), in the style it was (block or
    flow), everything else as it was. Raises MigrateError when the result
    wouldn't read back as intended, or a route has no new form."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise MigrateError("the file doesn't start with --- frontmatter")
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    if close < 0:
        raise MigrateError("the frontmatter has no closing ---")
    before, _ = parse_frontmatter(text)
    expected = _expected_auths(before)
    out = "\n".join([lines[0], *_rewrite_auth_lines(lines[1:close]), *lines[close:]])
    try:
        after, _ = parse_frontmatter(out)
    except YamlSubsetError as e:
        raise MigrateError(f"couldn't rewrite it safely ({e}); edit it by hand") from e
    if dict(after) != expected:
        raise MigrateError("couldn't rewrite the routes' auth safely (its layout is "
                           "unusual); edit it by hand")
    return out


def _expected_auths(before: Mapping[str, object]) -> dict[str, object]:
    """`before` with each old-format auth as `new_auth` makes it."""
    expected = dict(before)
    egress = before.get("egress")
    if not isinstance(egress, dict):
        return expected
    routes: list[object] = []
    for route in _routes(before):
        auth = _auth(route)
        if auth is None or not any(k in auth for k in _OLD_AUTH_KEYS) or \
                not isinstance(route, dict):
            routes.append(route)
            continue
        inspect = dict(as_json_object(route["inspect"], "inspect"))
        inspect["auth"] = new_auth(_host(route), auth)
        routes.append({**route, "inspect": inspect})
    expected["egress"] = {**egress, "routes": routes}
    return expected


__all__ = ["MigrateError", "Migration", "apply", "new_auth", "old_auths", "prepare", "rewrite",
           "rewrite_auth"]
