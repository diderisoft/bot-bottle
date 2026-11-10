"""Answer an agent's pending supervise requests from the terminal: summaries
for the status bar, the routes a request adds, and saving them to the project's
bottle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .contract.supervise import (
    TOOL_ADD_REPO,
    TOOL_CREATE_REPO,
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_EGRESS_TOKEN_ALLOW,
    TOOL_FORGE_CONNECT,
    TOOL_GITLEAKS_ALLOW,
    TOOL_LAUNCH_BOTTLE,
)
from .contract.yaml_subset import parse_frontmatter, serialize_yaml_subset
from .gateway.egress.schema import load_config
from .gateway.egress.types import Route
from .manifest import ManifestIndex, trust
from .manifest.egress import route_entries, with_entries
from .manifest.loader import PROJECT_BOTTLE, bottle_ref, load_bottle_chain
from .manifest.schema import DEFAULT_BOTTLE
from .manifest.versions import record_or_warn
from .paths import bot_bottle_root

_TOOL_WORDS = {
    TOOL_EGRESS_BLOCK: "wants to block a host",
    TOOL_GITLEAKS_ALLOW: "wants to push past a secret-scan finding",
    TOOL_EGRESS_TOKEN_ALLOW: "wants a credential sent to a host",
    TOOL_FORGE_CONNECT: "wants a git forge connected",
    TOOL_ADD_REPO: "wants a repo",
    TOOL_LAUNCH_BOTTLE: "wants to launch a child bottle",
    TOOL_CREATE_REPO: "wants a new repo created",
}


@dataclass(frozen=True)
class Request:
    """A pending proposal, as the CLI sees it."""

    id: str
    bottle: str  # orchestrator bottle id
    session: str  # tmux session name
    tool: str
    justification: str
    proposed: str
    current: str
    arrived: str = ""  # ISO timestamp
    # The orchestrator holding it, when listed from several; its answer goes
    # back there.
    orchestrator: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Request":
        return cls(id=str(d["id"]), bottle=str(d["bottle_slug"]),
                   session=str(d.get("bottle_label") or d["bottle_slug"]),
                   tool=str(d["tool"]), justification=str(d.get("justification", "")),
                   proposed=str(d.get("proposed_file", "")),
                   current=str(d.get("current_file", "")),
                   arrived=str(d.get("arrival_timestamp", "")),
                   orchestrator=str(d.get("orchestrator_url", "")))

    def new_routes(self) -> list[Route]:
        """The routes an egress-allow proposal adds or changes, by host."""
        if self.tool != TOOL_EGRESS_ALLOW:
            return []
        try:
            proposed = load_config(self.proposed).routes
            current = {r.host: r for r in load_config(self.current).routes} \
                if self.current.strip() else {}
        except Exception:  # pylint: disable=broad-exception-caught  # agent-written text
            return []
        return [r for r in proposed if r.host not in current
                or _comparable(current[r.host]) != _comparable(r)]

    def summary(self) -> str:
        """One line: what the agent wants."""
        routes = self.new_routes()
        if routes:
            first = routes[0]
            methods = sorted({m for entry in first.matches for m in entry.methods})
            what = f"{', '.join(methods) or 'any'} {first.host}"
            more = f" (+{len(routes) - 1} more)" if len(routes) > 1 else ""
            return f"{self.session} wants {what}{more}"
        return f"{self.session} {_TOOL_WORDS.get(self.tool, 'asks: ' + self.tool)}"

    def can_save_to_project(self) -> bool:
        """Only plain egress additions can be saved; a route carrying a
        credential is allowed once."""
        routes = self.new_routes()
        return bool(routes) and not any(r.auth_scheme or r.token_env for r in routes)


def _comparable(route: Route) -> tuple[object, ...]:
    """A route as plain data (its matches hold compiled regexes)."""
    return (manifest_route(route), route.auth_scheme, route.auth_header, route.token_env)


def manifest_route(route: Route) -> dict[str, object]:
    """A gateway route as a bottle-manifest route. A gateway entry's empty
    methods allow any (`["*"]`); no entries at all is GET/HEAD."""
    out: dict[str, object] = {"host": route.host}
    if not route.inspect:
        out["passthrough"] = True
        return out
    entries: list[dict[str, Any]] = []
    for entry in route.matches:
        m: dict[str, Any] = {}
        if entry.paths:
            m["paths"] = [p.value if p.type == "prefix" else {p.type: p.value}
                          for p in entry.paths]
        m["methods"] = list(entry.methods) or ["*"]
        if entry.headers:
            m["headers"] = [{"name": h.name, "value": h.value, "type": h.type}
                            for h in entry.headers]
        entries.append(m)
    if entries:
        out = with_entries(out, entries)
    if route.git_fetch:
        out["git"] = {"fetch": True}
    return out


def project_bottle_path(project: Path) -> Path:
    return project / ".bot-bottle" / "bottles" / f"{PROJECT_BOTTLE}.md"


def save_to_project(project: Path, routes: list[Route]) -> Path:
    """Add `routes` to the project's bottle (creating one that extends
    `basic`), replacing same-host routes, then trust the result."""
    path = project_bottle_path(project)
    _add_routes(path, routes)
    trust_project_bottle(project, path.stem)
    return path


@dataclass(frozen=True)
class KeepTarget:
    """Where "keep in bottle" writes: the project's `./main` when the bottle
    shared a project, else the first host bottle it was launched with."""

    path: Path
    project: Path | None

    def label(self) -> str:
        if self.project is not None:
            return f"./{PROJECT_BOTTLE} in {self.project.name}"
        return self.path.stem


def keep_target(session: str) -> KeepTarget | None:
    """Where a request from `session` can be kept, or None.

    Returns None for runs launched with an unsaved suggested bottle (ADR 0017):
    "keep" must not silently write into a stacked library bottle the user
    never chose to keep things into.
    """
    project = session_project(session)
    if project is not None:
        return KeepTarget(project_bottle_path(project), project)
    # pylint: disable-next=import-outside-toplevel
    from .bottle_state import read_metadata
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import RUN_BOTTLE
    metadata = read_metadata(session)
    host_bottles = bot_bottle_root() / "bottles"
    names = metadata.bottle_names if metadata else ()
    if metadata is not None and not names:
        names = (DEFAULT_BOTTLE,)
    # An unsaved run bottle is in the name list: the user launched a suggested
    # bottle without saving it.  Writing a stacked library bottle (e.g. the
    # forge bottle) on their behalf would be surprising and wrong.
    if RUN_BOTTLE in names:
        return None
    for name in names:
        if not name.startswith("./") and (host_bottles / f"{name}.md").is_file():
            return KeepTarget(host_bottles / f"{name}.md", None)
    return None



def keep(target: KeepTarget, request: "Request") -> Path:
    """Write the request's routes into `target`: the project's bottle (trusted,
    left for you to commit), or a host bottle as a new version naming the
    request."""
    routes = request.new_routes()
    if target.project is not None:
        return save_to_project(target.project, routes)
    _add_routes(target.path, routes)
    hosts = ", ".join(sorted({r.host for r in routes}))
    record_or_warn(f"keep egress-allow {hosts} (request {request.id[:8]})")
    return target.path


def keep_grant(target: KeepTarget, grant: str, *,
               forge: dict[str, object] | None = None) -> Path:
    """Write the repo grant into `target` for later launches, replacing one for
    the same repo name, with `forge` (its forge's description, ADR 0019) when
    the bottle doesn't describe it. Trusted for a project, a version for a
    host bottle."""
    fm, body = read_bottle(target.path)
    return save_bottle(target, bottle_text(with_grant(fm, grant, forge=forge), body),
                       f"keep add-repo {grant}")


def with_grant(frontmatter: dict[str, object], grant: str, *,
               forge: dict[str, object] | None = None) -> dict[str, object]:
    """`frontmatter` with the repo grant added (replacing one for the same repo
    name) and `forge`, the description of its forge, under `git-gate.forges`
    unless one is there.
    Supports `<alias>=<grant>` form; the alias becomes the key in the bottle
    file and the repos section is upgraded to a mapping when needed."""
    # pylint: disable-next=import-outside-toplevel
    from .manifest.forges import parse_grant, split_grant_alias
    fm = dict(frontmatter)
    git = fm.get("git-gate")
    git = dict(git) if isinstance(git, dict) else {}
    alias, bare_grant = split_grant_alias(grant)
    repo = alias or parse_grant(bare_grant, "the grant")[2]
    stored = bare_grant  # value kept in the bottle file is always the bare grant
    repos = git.get("repos")
    if alias:
        # Upgrade list repos to a dict when an alias needs a named key.
        if isinstance(repos, dict):
            git["repos"] = {**{k: v for k, v in repos.items() if k != repo}, repo: stored}
        else:
            mapped: dict[str, object] = {}
            for item in (repos if isinstance(repos, list) else []):
                if isinstance(item, str):
                    try:
                        n = parse_grant(item, "a kept grant")[2]
                        if n != repo:
                            mapped[n] = item
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
            git["repos"] = {**mapped, repo: stored}
    elif isinstance(repos, dict):
        git["repos"] = {**repos, repo: stored}
    else:
        kept = [g for g in repos if not (isinstance(g, str)
                                         and parse_grant(g, "a grant")[2] == repo)] \
            if isinstance(repos, list) else []
        git["repos"] = [*kept, stored]
    if forge:
        host = parse_grant(bare_grant, "the grant")[0]
        forges = git.get("forges")
        forges = dict(forges) if isinstance(forges, dict) else {}
        forges.setdefault(host, dict(forge))
        git["forges"] = forges
    fm["git-gate"] = git
    return fm


def merge_route(old: dict[str, object], new: dict[str, object]) -> dict[str, object]:
    """A host's route with another route for it merged in: what the old one
    allowed stays allowed (its settings too), plus what the new one allows. A
    route that already allows any request is left as it is."""
    old_entries = route_entries(old)
    if old_entries is None:
        return old  # any request already
    new_entries = route_entries(new)
    if new_entries is None:  # the new one allows any request
        return with_entries(old, None)
    return with_entries(old, old_entries + [e for e in new_entries if e not in old_entries])


def with_routes(frontmatter: dict[str, object], routes: list[Route]) -> dict[str, object]:
    """`frontmatter` with `routes` added to its egress. A host it already has a
    route for gets the new matches merged into that route (`merge_route`),
    so nothing it allowed is dropped; one it blocks gets the new route."""
    fm = dict(frontmatter)
    egress = fm.get("egress")
    egress = dict(egress) if isinstance(egress, dict) else {}
    existing = egress.get("routes")
    kept = [r for r in existing if isinstance(r, dict)] if isinstance(existing, list) else []
    for route in routes:
        new = manifest_route(route)
        at = next((i for i, r in enumerate(kept) if r.get("host") == route.host), None)
        if at is None:
            kept.append(new)
        elif kept[at].get("blocked"):  # allowing a blocked host unblocks it
            kept[at] = new
        else:
            kept[at] = merge_route(kept[at], new)
    egress["routes"] = kept
    fm["egress"] = egress
    return fm


_PROJECT_BODY = ("The bottle for this project. bot-bottle adds a route here when you\n"
                 "keep an allowed request in it; review changes like any other code.")


def read_bottle(path: Path) -> tuple[dict[str, object], str]:
    """A bottle file's frontmatter and body, or a new project bottle's."""
    if path.is_file():
        return parse_frontmatter(path.read_text())
    return {"extends": "basic"}, _PROJECT_BODY


def bottle_text(frontmatter: dict[str, object], body: str) -> str:
    """A bottle file's text."""
    return f"---\n{serialize_yaml_subset(frontmatter)}---\n\n{body.strip()}\n"


def save_bottle(target: KeepTarget, text: str, message: str) -> Path:
    """Write `text` to `target`, keeping the comments the file had: trusted
    for a project bottle (left for you to commit), a new version named
    `message` for a host bottle."""
    # pylint: disable-next=import-outside-toplevel
    from .contract.yaml_comments import keep_comments
    if target.path.is_file():
        text = keep_comments(target.path.read_text(), text)
    target.path.parent.mkdir(parents=True, exist_ok=True)
    target.path.write_text(text)
    if target.project is not None:
        trust_project_bottle(target.project, target.path.stem)
    else:
        record_or_warn(message)
    return target.path


def _add_routes(path: Path, routes: list[Route]) -> None:
    """Add `routes` to the bottle at `path` (creating a project bottle that
    extends `basic`), replacing same-host routes."""
    frontmatter, body = read_bottle(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bottle_text(with_routes(frontmatter, routes), body))


def trust_project_bottle(project: Path, name: str) -> None:
    """Trust the project bottle `name` (its file's stem), which bot-bottle just
    wrote."""
    index = ManifestIndex.resolve(str(project))
    name = bottle_ref(name, True)
    bottle, sources = load_bottle_chain(name, index.bottle_dirs())
    try:
        trust.check(name, bottle, sources)
    except trust.UntrustedBottleError as e:
        trust.trust(e)


ALLOW_ONCE, ALLOW_PROJECT, DENY = "allow_once", "allow_project", "deny"
DECISIONS = (ALLOW_ONCE, ALLOW_PROJECT, DENY)


def _forge_now_connected(client: Any, request: Request, via: str) -> str:
    """Allow an agent's `forge-connect` once the operator connected the forge
    another way (GitHub's sign-in, `forge connect` on the host): the console's
    "Done, connected". Allowing it before would tell the agent it has a
    forge it doesn't, so that raises ValueError."""
    # pylint: disable=import-outside-toplevel
    from urllib.parse import urlparse
    from . import forge_connections
    from .control.hostctl.nudge import nudge_if_waited
    host = urlparse(request.proposed.strip()).hostname or ""
    try:
        conn = forge_connections.get(host)
    except forge_connections.ForgeConnectionError as e:
        raise ValueError(str(e)) from e
    if conn is None:
        raise ValueError(f"{host or request.proposed} isn't connected on this host yet; "
                         f"connect it, then answer again")
    note = f"connected {host} as {conn.user().name}; ask for its repos with add-repo"
    client.supervise_respond(request.id, bottle_slug=request.bottle, decision="approve",
                             notes=f"{note}, from {via}")
    nudge_if_waited(request, "approved", note)
    return note


def answer(client: Any, request: Request, decision: str, *, via: str) -> str:
    """Act on `decision` through the orchestrator `client` and return what
    happened. "Allow for project" also saves the routes, or raises ValueError
    if the request can't be saved. `via` is noted on the decision."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    if request.tool == TOOL_FORGE_CONNECT and decision != DENY:
        return _forge_now_connected(client, request, via)
    if request.tool in (TOOL_ADD_REPO, TOOL_LAUNCH_BOTTLE, TOOL_CREATE_REPO) and decision != DENY:
        # Allowing alone would tell the agent it has what it asked for.
        raise ValueError("answer this request from the console's card, or deny it")
    target = keep_target(request.session) if decision == ALLOW_PROJECT else None
    if decision == ALLOW_PROJECT and (target is None or not request.can_save_to_project()):
        raise ValueError("this request can only be allowed once")
    # pylint: disable-next=import-outside-toplevel
    from .control.hostctl.nudge import nudge_if_waited
    if decision == DENY:
        client.supervise_respond(request.id, bottle_slug=request.bottle,
                                 decision="reject", notes=f"denied from {via}")
        nudge_if_waited(request, "denied")
        return "denied"
    note = f"allowed and kept in {target.label()}" if target is not None else "allowed once"
    client.supervise_respond(request.id, bottle_slug=request.bottle, decision="approve",
                             notes=f"{note}, from {via}")
    nudge_if_waited(request, "approved", note)
    if target is not None:
        saved = keep(target, request)
        where = (f"./{saved.relative_to(target.project)} in the project"
                 if target.project is not None else f"{saved} (a new version)")
        return f"allowed, and kept in {where}; it applies from the next launch"
    return "allowed once"


def route_lines(request: Request, *, host_width: int = 0,
                credential: Callable[[str], str] = lambda env: f"with ${env}") -> list[str]:
    """The routes a request adds, one line each: host (padded to `host_width`),
    methods, paths, git fetch, and the injected credential."""
    lines = []
    for route in request.new_routes():
        methods = sorted({m for entry in route.matches for m in entry.methods}) or ["any"]
        # Fields are two spaces apart, and the agent writes the paths: a run of
        # spaces in one would start a field of its making ("with $TOKEN").
        paths = sorted({" ".join(p.value.split()) for entry in route.matches for p in entry.paths})
        line = f"{route.host:<{host_width}}  {', '.join(methods)}"
        if paths:
            line += f"  paths {', '.join(paths)}"
        if route.git_fetch:
            line += "  + git clone/fetch"
        if route.token_env:
            line += f"  {credential(route.token_env)}"
        lines.append(line)
    return lines


def card_message(lines: list[str]) -> str:
    """An approval card's text: one line per line. The console reads it line
    by line, and some lines carry what the agent wrote (its why, a prompt, a
    repo name): a newline in those would start a line of the agent's making,
    one that could pass for the host's (an install link, a keep target, a
    route). Each line keeps to one."""
    return "\n".join(" ".join(line.splitlines()) for line in lines)


def session_project(session: str) -> Path | None:
    """The project directory a bottle was started on, if it shared one."""
    from .bottle_state import read_metadata  # pylint: disable=import-outside-toplevel
    metadata = read_metadata(session)
    if metadata is None or not metadata.cwd:
        return None
    path = Path(metadata.cwd)
    return path if path.is_dir() and path != Path(os.path.expanduser("~")) else None


__all__ = ["ALLOW_ONCE", "ALLOW_PROJECT", "DECISIONS", "DENY", "KeepTarget", "Request", "answer",
           "bottle_text", "card_message", "keep", "keep_grant", "keep_target", "manifest_route",
           "project_bottle_path", "read_bottle", "route_lines", "save_bottle", "save_to_project",
           "session_project", "trust_project_bottle", "with_grant", "with_routes"]
