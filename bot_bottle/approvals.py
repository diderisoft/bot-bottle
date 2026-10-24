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
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_EGRESS_TOKEN_ALLOW,
    TOOL_GITLEAKS_ALLOW,
)
from .contract.yaml_subset import parse_frontmatter, serialize_yaml_subset
from .gateway.egress.schema import load_config
from .gateway.egress.types import Route
from .manifest import ManifestIndex, trust
from .manifest.loader import load_bottle_chain
from .util import slugify

_TOOL_WORDS = {
    TOOL_EGRESS_BLOCK: "wants to block a host",
    TOOL_GITLEAKS_ALLOW: "wants to push past a secret-scan finding",
    TOOL_EGRESS_TOKEN_ALLOW: "wants a credential sent to a host",
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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Request":
        return cls(id=str(d["id"]), bottle=str(d["bottle_slug"]),
                   session=str(d.get("bottle_label") or d["bottle_slug"]),
                   tool=str(d["tool"]), justification=str(d.get("justification", "")),
                   proposed=str(d.get("proposed_file", "")),
                   current=str(d.get("current_file", "")),
                   arrived=str(d.get("arrival_timestamp", "")))

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
    return (manifest_route(route), route.auth_scheme, route.token_env)


def manifest_route(route: Route) -> dict[str, object]:
    """A gateway route as a bottle-manifest route."""
    inspect: dict[str, object] = {}
    matches = []
    for entry in route.matches:
        m: dict[str, object] = {}
        if entry.paths:
            m["paths"] = [{"type": p.type, "value": p.value} for p in entry.paths]
        if entry.methods:
            m["methods"] = list(entry.methods)
        if entry.headers:
            m["headers"] = [{"name": h.name, "value": h.value, "type": h.type}
                            for h in entry.headers]
        matches.append(m)
    if matches:
        inspect["matches"] = matches
    if route.git_fetch:
        inspect["git"] = {"fetch": True}
    return {"host": route.host, **({"inspect": inspect} if inspect else {})}


def project_bottle_path(project: Path) -> Path:
    return project / ".bot-bottle" / "bottles" / f"{slugify(project.name)}.md"


def save_to_project(project: Path, routes: list[Route]) -> Path:
    """Add `routes` to the project's bottle (creating one that extends
    `basic`), replacing same-host routes, then trust the result."""
    path = project_bottle_path(project)
    frontmatter: dict[str, object] = {"extends": "basic"}
    body = ("The bottle for this project. bot-bottle adds a route here when you\n"
            "allow a request \"for project\"; review changes like any other code.")
    if path.is_file():
        frontmatter, body = parse_frontmatter(path.read_text())
    egress = frontmatter.get("egress")
    egress = dict(egress) if isinstance(egress, dict) else {}
    existing = egress.get("routes")
    kept = [r for r in existing if isinstance(r, dict)] if isinstance(existing, list) else []
    hosts = {r.host for r in routes}
    kept = [r for r in kept if r.get("host") not in hosts]
    egress["routes"] = kept + [manifest_route(r) for r in routes]
    frontmatter["egress"] = egress
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{serialize_yaml_subset(frontmatter)}---\n\n{body.strip()}\n")
    _trust_project_bottle(project, path.stem)
    return path


def _trust_project_bottle(project: Path, name: str) -> None:
    index = ManifestIndex.resolve(str(project))
    bottle, sources = load_bottle_chain(name, index.bottle_dirs())
    try:
        trust.check(name, bottle, sources)
    except trust.UntrustedBottleError as e:
        trust.trust(e)


ALLOW_ONCE, ALLOW_PROJECT, DENY = "allow_once", "allow_project", "deny"
DECISIONS = (ALLOW_ONCE, ALLOW_PROJECT, DENY)


def answer(client: Any, request: Request, decision: str, *, via: str) -> str:
    """Act on `decision` through the orchestrator `client` and return what
    happened. "Allow for project" also saves the routes, or raises ValueError
    if the request can't be saved. `via` is noted on the decision."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    project = session_project(request.session) if decision == ALLOW_PROJECT else None
    if decision == ALLOW_PROJECT and (project is None or not request.can_save_to_project()):
        raise ValueError("this request can only be allowed once")
    if decision == DENY:
        client.supervise_respond(request.id, bottle_slug=request.bottle,
                                 decision="reject", notes=f"denied from {via}")
        return "denied"
    note = "allowed for the project" if project is not None else "allowed once"
    client.supervise_respond(request.id, bottle_slug=request.bottle, decision="approve",
                             notes=f"{note}, from {via}")
    if project is not None:
        saved = save_to_project(project, request.new_routes())
        return f"allowed, and saved to ./{saved.relative_to(project)} in the project"
    return "allowed once"


def route_lines(request: Request, *, host_width: int = 0,
                credential: Callable[[str], str] = lambda env: f"with ${env}") -> list[str]:
    """The routes a request adds, one line each: host (padded to `host_width`),
    methods, paths, git fetch, and the injected credential."""
    lines = []
    for route in request.new_routes():
        methods = sorted({m for entry in route.matches for m in entry.methods}) or ["any"]
        paths = sorted({p.value for entry in route.matches for p in entry.paths})
        line = f"{route.host:<{host_width}}  {', '.join(methods)}"
        if paths:
            line += f"  paths {', '.join(paths)}"
        if route.git_fetch:
            line += "  + git clone/fetch"
        if route.token_env:
            line += f"  {credential(route.token_env)}"
        lines.append(line)
    return lines


def session_project(session: str) -> Path | None:
    """The project directory a bottle was started on, if it shared one."""
    from .bottle_state import read_metadata  # pylint: disable=import-outside-toplevel
    metadata = read_metadata(session)
    if metadata is None or not metadata.cwd:
        return None
    path = Path(metadata.cwd)
    return path if path.is_dir() and path != Path(os.path.expanduser("~")) else None


__all__ = ["ALLOW_ONCE", "ALLOW_PROJECT", "DECISIONS", "DENY", "Request", "answer",
           "manifest_route", "project_bottle_path", "route_lines", "save_to_project",
           "session_project"]
