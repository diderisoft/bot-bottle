"""Answering an agent's requests from the terminal (PRD 0094, chunk 4).

An agent asks for more through supervise (`egress-allow` and friends); the
request waits in the orchestrator. This module turns a pending proposal into
what a person needs to decide: a one-line summary for the status bar, the
routes it would add, and, for "allow for project", those routes written into
the project's bottle so the next run starts with them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
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
    bottle: str  # the orchestrator's bottle id (what /supervise/respond takes)
    session: str  # the bottle's label: its tmux session
    tool: str
    justification: str
    proposed: str
    current: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Request":
        return cls(id=str(d["id"]), bottle=str(d["bottle_slug"]),
                   session=str(d.get("bottle_label") or d["bottle_slug"]),
                   tool=str(d["tool"]), justification=str(d.get("justification", "")),
                   proposed=str(d.get("proposed_file", "")),
                   current=str(d.get("current_file", "")))

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
        """Only plain egress additions go into a project bottle; a route that
        carries a credential is allowed once, never saved."""
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
    """Add ``routes`` to the project's bottle (creating it to extend
    `basic`), replacing a route for the same host, then trust the result:
    the person approving it is the one who'd be asked."""
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


def session_project(session: str) -> Path | None:
    """The project directory a bottle was started on, if it shared one."""
    from .bottle_state import read_metadata  # pylint: disable=import-outside-toplevel
    metadata = read_metadata(session)
    if metadata is None or not metadata.cwd:
        return None
    path = Path(metadata.cwd)
    return path if path.is_dir() and path != Path(os.path.expanduser("~")) else None


__all__ = ["Request", "manifest_route", "project_bottle_path", "save_to_project",
           "session_project"]
