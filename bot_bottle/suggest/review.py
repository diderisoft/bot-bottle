"""What a proposal lets the agent do, shaped for the guided start's review.

`glance` is the review at first sight: the architect's gist (checked; else a
sentence made here), what this run adds that no bottle already has, and what
needs doing before it can run. The gist only describes; the grants are always
listed from the proposal itself.

The full review (`d`) leads with what's new, one line each, then each bottle it builds
on as a line of counts with that bottle's about text, then the forges its
repos push to and as whom (ADR 0019). Every line carries what it permits in
brackets: HTTP methods for hosts, `push`/`read` for repos. `every_line`
lists each element on its own line, for the scrollable details.

It reads the bottle files themselves (as `bottle_elements` does), so each
element is attributed to the nearest file that declares it.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .. import secret_ref
from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter, serialize_yaml_subset
from .proposal import Proposal
from .validate import proposal_to_frontmatter, stack_names

NEW = "new"
_SUGGEST = "_suggest"
_UNCONNECTED = "not connected here"
_METHOD_ORDER = ("GET", "POST", "PUT", "PATCH", "DELETE")
_IMPLIED = {"HEAD", "OPTIONS"}  # read requests that come with GET


@dataclass(frozen=True)
class Line:
    """One review line: `label` (new, a bottle, forge; "" continues the one
    above), `what` (a host, a count), `detail` (why, or the bottle's about),
    `access` (in brackets), the `secret` it carries, and its `tone`: `new`,
    `write`, `read` or `warn`."""

    label: str
    what: str
    detail: str = ""
    access: str = ""
    secret: str = ""
    tone: str = "read"


@dataclass(frozen=True)
class Glance:
    """The review at first sight: `gist`, the `new` grants (`api.linear.app`,
    `acme/app (push)`, `secrets file LINEAR_TOKEN`), `connect`, each forge to connect
    before it can run, with the URL `forge connect` takes, and `warns`, any
    other forge trouble (its connections file doesn't read)."""

    gist: str
    new: list[str]
    warns: list[str]
    connect: dict[str, str] = field(default_factory=dict)


@dataclass
class _Host:
    host: str
    methods: set[str]
    secret: str = ""


@dataclass
class _Repo:
    name: str  # owner/repo, or the long form's name
    forge: str  # the grant's forge host; "" for a long-form repo
    write: bool


@dataclass
class _File:
    name: str
    about: str
    hosts: list[_Host] = field(default_factory=list)
    repos: list[_Repo] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    # Where each env var's value comes from (`secret_ref.source_label`):
    # "asks", "env VAR", "secrets file NAME"; absent for a literal.
    env_from: dict[str, str] = field(default_factory=dict)

    def env_access(self, var: str) -> str:
        """An env var's bracketed access: where its value comes from, else
        `env`."""
        return self.env_from.get(var) or "env"


def route_methods(route: dict[str, Any]) -> set[str]:
    """The HTTP methods a manifest egress route lets through ("any" for all;
    empty when it's blocked). An inspected route with no matches is read-only
    unless the gateway injects auth on it (manifest.md)."""
    if route.get("blocked") is True:
        return set()
    inspect = route.get("inspect")
    if inspect is False:
        return {"any"}
    inspect = inspect if isinstance(inspect, dict) else {}
    matches = [m for m in inspect.get("matches") or [] if isinstance(m, dict)]
    if not matches:
        return {"any"} if inspect.get("auth") or inspect.get("preserve_auth") else {"GET"}
    methods: set[str] = set()
    for match in matches:
        named = [str(m).upper() for m in match.get("methods") or []]
        if not named:
            return {"any"}
        methods.update(named)
    return methods


def short_methods(methods: set[str]) -> str:
    """`GET`, `GET, POST`, or `any`: HEAD and OPTIONS go with GET, and past
    three methods a route is as good as unrestricted."""
    if not methods or "any" in methods:
        return "any"
    shown = methods - _IMPLIED if "GET" in methods else set(methods)
    if len(shown) > 3:
        return "any"
    order = {m: i for i, m in enumerate(_METHOD_ORDER)}
    return ", ".join(sorted(shown, key=lambda m: (order.get(m, 9), m)))


def repo_names(repos: list[str]) -> str:
    """Repos as few words: `didericis/* ×8, acme/* ×2` when three owners or
    fewer hold more than one each, else names, `+N` past two."""
    owners = Counter(r.split("/", 1)[0] for r in repos if "/" in r)
    if len(repos) > 2 and 0 < len(owners) <= 3 and sum(owners.values()) == len(repos):
        return ", ".join(f"{o}/* ×{n}" if n > 1 else next(r for r in repos if r.startswith(f"{o}/"))
                         for o, n in owners.most_common())
    shown = ", ".join(repos[:2])
    return shown + (f" +{len(repos) - 2}" if len(repos) > 2 else "")


def _about(body: str) -> str:
    for line in body.strip().splitlines():
        if line.strip() and not line.startswith("#"):
            return line.strip().rstrip(".:,")
    return ""


def _secret(route: dict[str, Any]) -> str:
    """Where the route's injected credential comes from: `secrets file NAME`,
    for `value: secret:NAME` (or a not yet migrated `token_ref: NAME`, which
    the review still shows)."""
    inspect = route.get("inspect")
    auth = inspect.get("auth") if isinstance(inspect, dict) else None
    if not isinstance(auth, dict):
        return ""
    value = auth.get("value")
    if isinstance(value, str) and secret_ref.kind(value) == secret_ref.HOST_ENV:
        return f"secret env {secret_ref.host_var(value)}"
    ref = secret_ref.stored_name(value) if isinstance(value, str) else ""
    ref = ref or auth.get("token_ref") or ""
    return f"secrets file {str(ref).lstrip('$')}" if ref else ""


def _repo(name: object, value: object) -> _Repo | None:
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.forges import parse_grant
    from ..manifest.util import ManifestError  # pylint: disable=import-outside-toplevel
    if isinstance(value, str):
        try:
            forge, owner, repo, write = parse_grant(value, "a grant")
        except ManifestError:
            return None
        return _Repo(f"{owner}/{repo}", forge, write)
    if isinstance(value, dict):
        key = value.get("key")
        read_only = isinstance(key, dict) and key.get("read_only") is True
        return _Repo(str(name), "", not read_only)
    return None


def _raw_chain(names: list[str], dirs: list[tuple[Path, bool]]) -> list[tuple[str, str]]:
    """Each file of bottles `names` (merged in that order, so the last
    wins) as (name, text), nearest first, following `extends` without
    validating: a bottle that wouldn't load (a forge this host hasn't
    connected) still shows what it asks for."""
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.loader import bottle_ref, find_bottle
    out: list[tuple[str, str]] = []
    seen: set[Path] = set()

    def visit(name: str, after: Path | None) -> None:
        # A bottle extending its own name (a shadow of a built-in) means the next one.
        rest = dirs[[d for d, _ in dirs].index(after) + 1:] if after in [d for d, _ in dirs] \
            else dirs
        found = find_bottle(name, rest)
        if found is None or found[0] in seen:
            return
        path, project = found
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
        except (OSError, YamlSubsetError):
            return
        out.append((bottle_ref(path.stem, project), text))
        extends = fm.get("extends")
        parents = [extends] if isinstance(extends, str) else (
            [str(x) for x in extends] if isinstance(extends, list) else [])
        for parent in reversed(parents):
            visit(parent, path.parent if parent == path.stem else None)

    for name in reversed(names):
        visit(name, None)
    return out


def _files(proposal: Proposal, dirs: list[tuple[Path, bool]]) -> list[_File] | None:
    """The files a launch with `proposal` reads, nearest first, each with the
    elements it's the nearest to declare. `_suggest` holds the additions."""
    fm = proposal_to_frontmatter(proposal)
    with tempfile.TemporaryDirectory() as tmp:
        bottles = Path(tmp) / "bottles"
        bottles.mkdir()
        if fm:
            (bottles / f"{_SUGGEST}.md").write_text(
                f"---\n{serialize_yaml_subset(fm)}---\n", encoding="utf-8")
            texts = _raw_chain(stack_names(proposal, _SUGGEST), [(bottles, False), *dirs])
        else:
            texts = _raw_chain(list(proposal.bottles) or ["basic"], dirs)
    if not texts:
        return None
    seen: set[str] = set()
    files: list[_File] = []
    for name, text in texts:
        fm_, body = parse_frontmatter(text)
        files.append(_elements(name, dict(fm_), body, seen))
    return files


def _elements(name: str, fm: dict[str, Any], body: str, seen: set[str]) -> _File:
    """`name`'s elements not declared by a nearer file (`seen`, updated)."""
    out = _File(name=NEW if name == _SUGGEST else name, about=_about(body))
    egress = fm.get("egress")
    for route in (egress.get("routes") or []) if isinstance(egress, dict) else []:
        if not isinstance(route, dict) or not route.get("host") or f"h:{route['host']}" in seen:
            continue
        seen.add(f"h:{route['host']}")
        methods = route_methods(route)
        if methods:  # a blocked route takes an inherited host away
            out.hosts.append(_Host(str(route["host"]), methods, _secret(route)))
    git = fm.get("git-gate") if isinstance(fm.get("git-gate"), dict) else {}
    repos = git.get("repos") if isinstance(git, dict) else None
    items = (list(repos.items()) if isinstance(repos, dict)
             else [("", g) for g in repos] if isinstance(repos, list) else [])
    for key, value in items:
        repo = _repo(key, value)
        if repo and f"r:{repo.name}" not in seen:
            seen.add(f"r:{repo.name}")
            out.repos.append(repo)
    env = fm.get("env")
    for var, value in (env if isinstance(env, dict) else {}).items():
        if f"e:{var}" not in seen:
            seen.add(f"e:{var}")
            out.env.append(str(var))
            source = secret_ref.source_label(value) if isinstance(value, str) else ""
            if source:
                out.env_from[str(var)] = source
    return out


def _new_lines(f: _File, proposal: Proposal) -> list[Line]:
    why_host = {e.host: e.why for e in proposal.additions.egress}
    why_repo = {r.grant.removesuffix(":write").split("/", 1)[-1]: r.why
                for r in proposal.additions.repos}
    why_env = {v.name: v.why for v in proposal.additions.env}
    lines = [Line("", h.host, why_host.get(h.host, ""), short_methods(h.methods), h.secret, NEW)
             for h in f.hosts]
    lines += [Line("", r.name, why_repo.get(r.name, ""), "push" if r.write else "read", "", NEW)
              for r in f.repos]
    lines += [Line("", v, why_env.get(v, ""), f.env_access(v), "", NEW) for v in f.env]
    return lines


def _counted(n: int, one: str) -> str:
    return f"{n} {one}{'' if n == 1 else 's'}"


def _bottle_lines(f: _File) -> list[Line]:
    lines: list[Line] = []
    if f.hosts:
        methods = short_methods(set().union(*(h.methods for h in f.hosts)))
        secrets = sorted({h.secret for h in f.hosts if h.secret})
        secret = secrets[0] if len(secrets) == 1 else _counted(len(secrets), "secret") if secrets \
            else ""
        lines.append(Line("", _counted(len(f.hosts), "host"), f.about, methods, secret,
                          "read" if methods == "GET" else "write"))
    for write in (True, False):
        repos = [r.name for r in f.repos if r.write == write]
        if repos:
            lines.append(Line("", _counted(len(repos), "repo"), repo_names(repos),
                              "push" if write else "read", "", "write" if write else "read"))
    if f.env:
        lines.append(Line("", _counted(len(f.env), "env var"), ", ".join(f.env[:3])
                          + (f" +{len(f.env) - 3}" if len(f.env) > 3 else "")))
    if not lines:
        lines.append(Line("", "nothing beyond its parents", f.about))
    return lines


def _forge_lines(files: list[_File]) -> list[Line]:
    """Each forge the repos are on: whom this host pushes as there, or that it
    isn't connected here."""
    # pylint: disable-next=import-outside-toplevel
    from .. import forge_connections
    hosts = sorted({r.forge for f in files for r in f.repos if r.forge})
    try:
        found = forge_connections.connections()
    except forge_connections.ForgeConnectionError as e:
        return [Line("forge", h, str(e), tone="warn") for h in hosts]
    lines = []
    for host in hosts:
        conn = found.get(host)
        if conn is None:
            lines.append(Line("forge", host, f"{_UNCONNECTED}; connect it with "
                                             f"`bot-bottle forge connect https://{host}`",
                              tone="warn"))
            continue
        try:
            user = conn.user()
        except forge_connections.ForgeConnectionError as e:
            lines.append(Line("forge", host, str(e), tone="warn"))
            continue
        lines.append(Line("forge", host, f"as {user.name}"
                          + (f" over {user.transport}" if user.transport else "")))
    return [Line("forge" if i == 0 else "", *rest) for i, (_, *rest) in
            enumerate((x.label, x.what, x.detail, x.access, x.secret, x.tone) for x in lines)]


def review(proposal: Proposal, dirs: list[tuple[Path, bool]]) -> list[Line] | None:
    """The review's lines: new, each bottle built on, the forges. None when
    the bottles can't be read."""
    files = _files(proposal, dirs)
    if files is None:
        return None
    lines: list[Line] = []
    for f in files:
        group = _new_lines(f, proposal) if f.name == NEW else _bottle_lines(f)
        if group:
            lines.append(Line(f.name, group[0].what, group[0].detail, group[0].access,
                              group[0].secret, group[0].tone))
            lines += group[1:]
    return lines + _forge_lines(files)


# A host name in the architect's gist: one this proposal doesn't reach means
# the gist describes something it didn't grant, so a made one replaces it.
_HOSTNAME = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
# A file the task names (`forecast.md`) isn't a host.
_FILE_EXTENSIONS = frozenset(
    "md txt rst py js ts tsx jsx json yaml yml toml ini cfg conf lock sh rs go rb java kt "
    "c h cpp hpp cs php html css scss csv tsv sql log xml svg png jpg gif pdf zip gz".split())
_GIST_MAX = 160


def gist(proposal: Proposal, hosts: set[str]) -> str:
    """The architect's gist when it's short and names only `hosts`, else the
    sentence `render.summary_line` makes from the proposal."""
    # pylint: disable-next=import-outside-toplevel
    from .render import summary_line
    text = proposal.summary
    named = [m.lower() for m in _HOSTNAME.findall(text)]
    if text and len(text) <= _GIST_MAX and all(
            m in hosts or m.rsplit(".", 1)[-1] in _FILE_EXTENSIONS for m in named):
        return text
    return summary_line(replace(proposal, summary=""))


def glance(proposal: Proposal, dirs: list[tuple[Path, bool]]) -> Glance | None:
    """The gist, the new grants and the warnings. None when the bottles can't
    be read."""
    files = _files(proposal, dirs)
    if files is None:
        return None
    new: list[str] = []
    for f in files:
        if f.name == NEW:
            new += [h.host for h in f.hosts]
            new += [r.name + (" (push)" if r.write else "") for r in f.repos]
            new += f.env
            new += sorted({h.secret for h in f.hosts if h.secret})
    hosts = {h.host for f in files for h in f.hosts} | {
        r.forge for f in files for r in f.repos if r.forge}
    # pylint: disable-next=import-outside-toplevel
    from .forges import connect_url
    warns: list[str] = []
    connect: dict[str, str] = {}
    for x in _forge_lines(files):
        if x.tone != "warn":
            continue
        if x.detail.startswith(_UNCONNECTED):
            connect[x.what] = connect_url(x.what, proposal.forges.get(x.what, {}))
        else:
            warns.append(f"{x.what}: {x.detail}")
    return Glance(gist(proposal, {h.lower() for h in hosts}), new, warns, connect)


def pending_glance(proposal: Proposal, dirs: list[tuple[Path, bool]]) -> Glance | None:
    """`glance` with the proposal's pending edits applied as if accepted
    (`edits.overlay`), its `connect` also naming the forges the first edit
    waits on."""
    # pylint: disable-next=import-outside-toplevel
    from . import edits
    from .forges import connect_url  # pylint: disable=import-outside-toplevel
    with edits.overlay(proposal.edits, dirs, proposal.forges) as seen_dirs:
        seen = glance(proposal, seen_dirs)
    if seen is None:
        return None
    for edit in proposal.edits[:1]:
        for host in edits.unconnected(edit):
            seen.connect.setdefault(host, connect_url(host, proposal.forges.get(host, {})))
    return seen


def every_line(proposal: Proposal, dirs: list[tuple[Path, bool]]) -> list[Line] | None:
    """Every host, repo and env var on its own line, under its bottle."""
    files = _files(proposal, dirs)
    if files is None:
        return None
    lines: list[Line] = []
    for f in files:
        rows = ([Line("", h.host, "", short_methods(h.methods), h.secret,
                      "read" if short_methods(h.methods) == "GET" else "write") for h in f.hosts]
                + [Line("", r.name, r.forge, "push" if r.write else "read", "",
                        "write" if r.write else "read") for r in f.repos]
                + [Line("", v, "", f.env_access(v)) for v in f.env])
        if f.name == NEW:
            rows = [Line("", r.what, r.detail, r.access, r.secret, NEW) for r in rows]
        if rows:
            lines.append(Line(f.name, rows[0].what, rows[0].detail, rows[0].access,
                              rows[0].secret, rows[0].tone))
            lines += rows[1:]
    return lines + _forge_lines(files)


__all__ = ["Glance", "Line", "NEW", "every_line", "gist", "glance", "pending_glance",
           "repo_names", "review",
           "route_methods", "short_methods"]
