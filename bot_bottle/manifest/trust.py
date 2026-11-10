"""Per-host trust for project bottles (`./.bot-bottle/bottles/`) and project
agents (`./.bot-bottle/agents/`).

A project bottle runs only once this host has trusted the hash of the project
files in its chain; a project agent whose provider grants something, or that
runs on one of the user's bottles, the hash of its file, of the files its
image build takes from the project, and of what it gets from the host. Any
change needs trusting again. A project bottle's trust also covers this host's
connection to each forge its repos are on (ADR 0019: where pushes go, and as
whom), so reconnecting a forge differently asks again. The user's own
manifests and the built-in `basic` need none.
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

from .. import secret_ref
from ..contract.forges import FORGE_KINDS
from ..paths import bot_bottle_root
from .agent import ManifestAgentProvider
from .bottle import ManifestBottle
from .loader import BUILTIN_BOTTLES_DIR, BottleSource, load_bottle_chain
from .util import ManifestError

TRUST_FILE = "trusted-bottles.json"

# A trust record's scheme. 2: a project agent's hash covers its image build
# inputs. Records without one are 1, from before that.
SCHEME = 2


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
            what += f", with secrets file {route.TokenRef}"
        lines.append(f"reach   {route.Host:<30} {what}")
    for repo in bottle.git:
        line = f"{'fetch' if repo.Key.read_only else 'push':<7} {repo.Upstream}"
        if repo.Key.provider in FORGE_KINDS:
            # The forge token goes to the API that mints the key.
            api = repo.Key.api_url or f"https://{repo.UpstreamHost}"
            line += f" (key minted at {api} with ${repo.Key.forge_token_env})"
        lines.append(line)
    for where in bottle.git_create:
        lines.append(f"create  repos at {where}")
    for name, value in sorted(bottle.env.items()):
        # Every secret kind (`secret_ref`): "?"/"?<message>" asks, "${VAR}"
        # reads the env, "secret:NAME" the secrets file.
        source = secret_ref.source_label(value)
        if source:
            lines.append(f"read    {name} ({source})")
    if bottle.nested_containers:
        lines.append("run     containers inside the bottle")
    return lines


def _fingerprint(host_key: str) -> str:
    """An SSH host key as OpenSSH shows it: `SHA256:<base64>`."""
    # pylint: disable-next=import-outside-toplevel
    import base64
    try:
        blob = base64.b64decode(host_key.split()[1], validate=True)
    except (IndexError, ValueError):
        return host_key
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def forge_lines(bottle: ManifestBottle) -> list[str]:
    """This host's connection to each forge `bottle`'s repos are on, one line
    each: what trusting a project bottle records of it."""
    used = {r.Key.api_url for r in bottle.git if r.Key.provider in FORGE_KINDS}
    lines: list[str] = []
    for forge in bottle.forges:
        if forge.url not in used:
            continue
        line = f"forge   {forge.name}: {forge.kind} at {forge.url}, ssh {forge.ssh}"
        if forge.host_key:
            line += f", host key {_fingerprint(forge.host_key)}"
        line += f", as {forge.account or '?'}"
        if forge.kind == "github":
            line += f" over {forge.transport or 'https'}"
        lines.append(line)
    return lines


def provider_grants(provider: ManifestAgentProvider) -> list[str]:
    """What an agent's provider grants, one line each. Picking a built-in
    harness grants nothing."""
    lines: list[str] = []
    if provider.forward_host_credentials or provider.auth_token:
        lines.append(f"use     your {provider.harness} login")
    base_url = provider.settings.get("base_url")
    key_env = provider.settings.get("api_key_env")
    if key_env and not base_url:
        lines.append(f"use     ${key_env} as your {provider.harness} API key")
    if isinstance(base_url, str) and base_url:
        host = urlparse(base_url).hostname or base_url
        lines.append(f"reach   {host}" + (f" with ${key_env}" if key_env else ""))
    if provider.dockerfile:
        lines.append(f"build   image from {provider.dockerfile}")
    if (provider.overlay.apt or provider.overlay.files or
            provider.overlay.harness_version or provider.overlay.dockerfile):
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
    scheme: int = 1


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
            scheme = e.get("scheme", 1)
            out.append(Trusted(str(e.get("hash", "")), str(e.get("bottle", "")),
                               str(e.get("path", "")), tuple(e.get("grants") or ()),
                               str(e.get("trusted_at", "")),
                               scheme if isinstance(scheme, int) else 1))
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
    connected = forge_lines(bottle)
    granted += connected
    digest = bottle_hash(sources)
    if connected:
        digest = hashlib.sha256("\0".join([digest, *connected]).encode()).hexdigest()
    entries = load_trusted()
    if any(e.hash == digest for e in entries):
        return
    top = str(project[0].path)
    previous = next((e for e in reversed(entries) if e.path == top), None)
    raise UntrustedBottleError(bottle_name, project[0].path, digest=digest, granted=granted,
                               previous=previous)


def agent_hash(name: str, path: Path, context: Sequence[str] = (), build: str = "") -> str:
    """sha256 over a project agent file (name and bytes), `context` (what it
    gets from the host beyond its provider) and `build` (`build_digest`), so a
    change to any of them asks again. With no build inputs it is the hash
    from before builds were covered, so those agents keep their trust."""
    digest = hashlib.sha256(b"agent\0" + name.encode() + b"\0" + path.read_bytes())
    for line in context:
        digest.update(b"\0" + line.encode())
    if build:
        digest.update(b"\0build\0" + build.encode())
    return digest.hexdigest()


def build_digest(provider: ManifestAgentProvider, source_root: Path) -> str:
    """Digest of what the agent's image build takes from the project, paths
    resolved from `source_root` (the launch directory) as the build does:
    the custom `dockerfile`'s bytes, and the overlay's staged context (its
    Dockerfile and that Dockerfile's directory, or its `files`). Empty when
    the agent builds nothing. A missing custom Dockerfile digests as missing,
    so adding one asks again."""
    # Imported here: agent_overlay imports this package.
    from .. import agent_overlay  # pylint: disable=import-outside-toplevel
    parts: list[str] = []
    if provider.dockerfile:
        path = Path(os.path.expanduser(provider.dockerfile))
        if not path.is_absolute():
            path = source_root / path
        try:
            parts.append("dockerfile\0" + hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            parts.append("dockerfile\0missing")
    overlay = agent_overlay.inputs_digest(
        provider.overlay, harness=provider.harness, source_root=source_root)
    if overlay:
        parts.append("overlay\0" + overlay)
    if not parts:
        return ""
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _host_bottle(sources: Sequence[BottleSource]) -> bool:
    """Whether a chain includes one of the user's bottles (not the project's,
    not a built-in)."""
    return any(not s.project and BUILTIN_BOTTLES_DIR not in s.path.parents for s in sources)


def host_context(name: str, *, home_agent: Path | None = None, bottle_name: str = "",
                 bottle: ManifestBottle | None = None,
                 sources: Sequence[BottleSource] = ()) -> list[str]:
    """What a project agent gets from the host beyond its provider: the user's
    bottle it runs on, and the user's agent it replaces."""
    lines: list[str] = []
    if bottle is not None and _host_bottle(sources):
        lines += [f"use     your bottle {bottle_name}: {g}"
                  for g in beyond_basic(grants(bottle))]
    if home_agent is not None:
        lines.append(f"replace your agent {name} ({home_agent})")
    return lines


def check_agent(name: str, path: Path, provider: ManifestAgentProvider, *,
                home_agent: Path | None = None, bottle_name: str = "",
                bottle: ManifestBottle | None = None,
                sources: Sequence[BottleSource] = (),
                source_root: Path | None = None) -> None:
    """Raise UntrustedBottleError unless the project agent at `path` is
    trusted. It needs trust when its provider grants something or it runs on
    one of the user's bottles that grants more than `basic`; replacing the
    user's agent of the same name is shown, not a reason on its own.
    `source_root` is the launch directory its build paths resolve from;
    default, the project holding `path` (`<root>/.bot-bottle/agents/`)."""
    context = host_context(name, home_agent=home_agent, bottle_name=bottle_name,
                           bottle=bottle, sources=sources)
    granted = provider_grants(provider) + context
    if not [g for g in granted if not g.startswith("replace ")]:
        return
    root = source_root if source_root is not None else path.parent.parent.parent
    digest = agent_hash(name, path, context, build_digest(provider, root))
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
                           time.strftime("%Y-%m-%d"), SCHEME))
    _save(entries)


def prompt_parts(error: UntrustedBottleError) -> tuple[list[str], str]:
    """The trust question's lines (what the bottle grants, or what changed)
    and the question itself."""
    if error.previous is None:
        lead = ("  Beyond the basic bottle, it lets the agent:" if error.kind == "bottle"
                else "  It lets the agent:")
        lines = [f"untrusted {error.kind}: {error.bottle}  ({error.path})", "", lead]
        lines += [f"    {g}" for g in error.grants]
        return lines, f"Trust this {error.kind} on this host?"
    before, after = set(error.previous.grants), set(error.grants)
    lines = [f"{error.kind} changed: {error.bottle}  ({error.path})",
             f"  trusted {error.previous.trusted_at}", ""]
    builds = any(g.startswith("build ") for g in error.grants)
    if error.kind == "agent" and builds and error.previous.scheme < 2:
        lines += ["  Trust now also covers the files its image build takes from the",
                  "  project (the Dockerfile and its directory, or the overlay files),",
                  "  which your earlier trust didn't. Review them before trusting.", ""]
    added = [g for g in error.grants if g not in before]
    removed = [g for g in error.previous.grants if g not in after]
    if added or removed:
        lines.append("  Now:")
        lines += [f"  + {g}" for g in added] + [f"  - {g}" for g in removed]
    else:
        lines.append("  It grants the same as before; the file changed in other ways."
                     if error.kind == "bottle" else
                     "  It grants the same as before; the file or its build inputs changed.")
    return lines, f"Trust the changed {error.kind}?"


def prompt_text(error: UntrustedBottleError) -> str:
    """The trust question as the terminal asks it."""
    lines, question = prompt_parts(error)
    return "\n".join([*lines, "", f"{question} [y/N] "])


__all__ = ["UntrustedBottleError", "agent_hash", "beyond_basic", "bottle_hash", "build_digest",
           "check",
           "check_agent", "forge_lines", "grants", "host_context", "load_trusted",
           "prompt_parts", "prompt_text",
           "provider_grants", "trust"]
