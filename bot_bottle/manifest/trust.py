"""Per-host trust for project bottles (`./.bot-bottle/bottles/`) and project
agents (`./.bot-bottle/agents/`) that declare an `agent_provider`.

A project bottle runs only once this host has trusted the hash of the project
files in its chain; a project agent, the hash of its file. Any change needs
trusting again. The user's own manifests and the built-in `basic` need none.
`trusted-bottles.json` (0600) keeps each hash with the manifest's path and
grants, to show what changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..paths import bot_bottle_root
from .agent import ManifestAgentProvider
from .bottle import ManifestBottle
from .loader import BUILTIN_BOTTLES_DIR, BottleSource, load_bottle_chain
from .util import ManifestError

TRUST_FILE = "trusted-bottles.json"


def bottle_hash(sources: Sequence[BottleSource]) -> str:
    """sha256 over the chain's project files (name and bytes): moving the repo
    keeps the trust, any edit loses it."""
    digest = hashlib.sha256()
    for source in sorted((s for s in sources if s.project), key=lambda s: s.name):
        digest.update(source.name.encode() + b"\0" + source.path.read_bytes() + b"\0")
    return digest.hexdigest()


def grants(bottle: ManifestBottle) -> list[str]:
    """What a bottle grants, one line each, for the trust prompt."""
    lines: list[str] = []
    for route in bottle.egress.routes:
        methods = sorted({m for match in route.Matches for m in match.Methods})
        what = ", ".join(methods) if methods else "any method"
        if route.GitFetch:
            what += ", git clone/fetch"
        if route.TokenRef:
            what += f", with ${route.TokenRef}"
        lines.append(f"reach   {route.Host:<30} {what}")
    for repo in bottle.git:
        lines.append(f"push    {repo.Upstream}")
    for name, value in sorted(bottle.env.items()):
        if "${" in value or value == "?":
            lines.append(f"read    {name} (from your environment or secrets)")
    if bottle.nested_containers:
        lines.append("run     containers inside the bottle")
    return lines


def provider_grants(provider: ManifestAgentProvider) -> list[str]:
    """What an agent's provider grants, one line each. Picking a built-in
    harness grants nothing."""
    lines: list[str] = []
    if provider.forward_host_credentials or provider.auth_token:
        lines.append(f"use     your {provider.harness} login")
    base_url = provider.settings.get("base_url")
    key_env = provider.settings.get("api_key_env")
    if isinstance(base_url, str) and base_url:
        host = urlparse(base_url).hostname or base_url
        lines.append(f"reach   {host}" + (f" with ${key_env}" if key_env else ""))
    for dockerfile in (provider.dockerfile, provider.overlay.dockerfile):
        if dockerfile:
            lines.append(f"build   image from {dockerfile}")
    if provider.overlay.apt or provider.overlay.files or provider.overlay.harness_version:
        lines.append("build   image overlay (packages, files or harness version)")
    return lines


def beyond_basic(granted: list[str]) -> list[str]:
    """`granted` minus what `basic` already allows."""
    basic, _ = load_bottle_chain("basic", ((BUILTIN_BOTTLES_DIR, False),))
    known = set(grants(basic))
    return [g for g in granted if g not in known]


@dataclass(frozen=True)
class Trusted:
    hash: str
    bottle: str
    path: str
    grants: tuple[str, ...]
    trusted_at: str


class UntrustedBottleError(ManifestError):
    """A project bottle (or, with `kind="agent"`, a project agent) this host
    hasn't trusted, or that changed since. `bottle` is the manifest's name."""

    def __init__(self, bottle: str, path: Path, *, digest: str, granted: list[str],
                 previous: Trusted | None, kind: str = "bottle") -> None:
        self.bottle = bottle
        self.path = path
        self.hash = digest
        self.grants = granted
        self.previous = previous
        self.kind = kind
        what = "changed since it was trusted" if previous else "isn't trusted on this host"
        super().__init__(
            f"{kind} {bottle} ({path}) {what}; review it with "
            f"`bot-bottle trust {bottle}` in that project")


def _trust_path() -> Path:
    return bot_bottle_root() / TRUST_FILE


def load_trusted() -> list[Trusted]:
    try:
        raw = json.loads(_trust_path().read_text())
    except (OSError, ValueError):
        return []
    entries = raw.get("trusted") if isinstance(raw, dict) else None
    out: list[Trusted] = []
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict):
            out.append(Trusted(str(e.get("hash", "")), str(e.get("bottle", "")),
                               str(e.get("path", "")), tuple(e.get("grants") or ()),
                               str(e.get("trusted_at", ""))))
    return out


def _save(entries: list[Trusted]) -> None:
    path = _trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"trusted": [e.__dict__ for e in entries]}, indent=2) + "\n"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    tmp.replace(path)


def check(bottle_name: str, bottle: ManifestBottle, sources: Sequence[BottleSource]) -> None:
    """Raise UntrustedBottleError unless the chain's project files are trusted."""
    project = [s for s in sources if s.project]
    if not project:
        return
    granted = beyond_basic(grants(bottle))
    if not granted:
        return  # nothing beyond basic
    digest = bottle_hash(sources)
    entries = load_trusted()
    if any(e.hash == digest for e in entries):
        return
    top = str(project[0].path)
    previous = next((e for e in reversed(entries) if e.path == top), None)
    raise UntrustedBottleError(bottle_name, project[0].path, digest=digest, granted=granted,
                               previous=previous)


def agent_hash(name: str, path: Path) -> str:
    """sha256 over a project agent file (name and bytes)."""
    return hashlib.sha256(b"agent\0" + name.encode() + b"\0" + path.read_bytes()).hexdigest()


def check_agent(name: str, path: Path, provider: ManifestAgentProvider) -> None:
    """Raise UntrustedBottleError unless the project agent at `path` is
    trusted. One whose provider grants nothing needs no trust."""
    granted = provider_grants(provider)
    if not granted:
        return
    digest = agent_hash(name, path)
    entries = load_trusted()
    if any(e.hash == digest for e in entries):
        return
    previous = next((e for e in reversed(entries) if e.path == str(path)), None)
    raise UntrustedBottleError(name, path, digest=digest, granted=granted,
                               previous=previous, kind="agent")


def trust(error: UntrustedBottleError) -> None:
    """Trust the bottle in `error` on this host, replacing an earlier version."""
    entries = [e for e in load_trusted() if e.path != str(error.path)]
    entries.append(Trusted(error.hash, error.bottle, str(error.path), tuple(error.grants),
                           time.strftime("%Y-%m-%d")))
    _save(entries)


def prompt_text(error: UntrustedBottleError) -> str:
    """The trust question: what the bottle grants, or what changed."""
    if error.previous is None:
        lead = ("  Beyond the basic bottle, it lets the agent:" if error.kind == "bottle"
                else "  Its provider lets the agent:")
        lines = [f"untrusted {error.kind}: {error.bottle}  ({error.path})", "", lead]
        lines += [f"    {g}" for g in error.grants]
        lines += ["", f"Trust this {error.kind} on this host? [y/N] "]
        return "\n".join(lines)
    before, after = set(error.previous.grants), set(error.grants)
    lines = [f"{error.kind} changed: {error.bottle}  ({error.path})",
             f"  trusted {error.previous.trusted_at}", ""]
    added = [g for g in error.grants if g not in before]
    removed = [g for g in error.previous.grants if g not in after]
    if added or removed:
        lines.append("  Now:")
        lines += [f"  + {g}" for g in added] + [f"  - {g}" for g in removed]
    else:
        lines.append("  It grants the same as before; the file changed in other ways.")
    lines += ["", f"Trust the changed {error.kind}? [y/N] "]
    return "\n".join(lines)


__all__ = ["UntrustedBottleError", "agent_hash", "beyond_basic", "bottle_hash", "check",
           "check_agent", "grants", "load_trusted", "prompt_text", "provider_grants", "trust"]
