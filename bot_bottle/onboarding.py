"""Bottle onboarding (PRD 0103): the first `bot-bottle` in a project with no
bottle of its own drafts one from the checkout, shows it, and writes it as
the project's `./main`, trusted on this host since bot-bottle wrote it.

The draft comes from the checkout, never from a model: its `origin`, read-only,
when this host connected the forge it's on (connecting it first if the user
wants), on top of `basic`'s package registries and public code. The draft
describes the forge too, so the bottle says where it goes on any machine
(ADR 0019).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .contract.yaml_subset import serialize_yaml_subset
from .forge_connect import ssh_host_port
from .manifest.loader import PROJECT_BOTTLE, bottle_ref

_SCP_RE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")


@dataclass(frozen=True)
class Origin:
    host: str
    port: str          # "" when the URL gives none
    owner: str
    repo: str
    ssh: bool = False  # an SSH remote: `port` is the SSH port, not the web one


@dataclass(frozen=True)
class ForgeMatch:
    """The connected forge `origin` is on: its host, and how a bottle
    describes it."""

    key: str
    description: tuple[tuple[str, object], ...] = ()


@dataclass
class Draft:
    origin: Origin | None
    forge: ForgeMatch | None
    writable: bool = False

    def grant(self) -> str:
        assert self.origin and self.forge
        return (f"{self.forge.key}/{self.origin.owner}/{self.origin.repo}"
                + (":write" if self.writable else ""))

    def frontmatter(self) -> dict[str, object]:
        if not (self.origin and self.forge):
            return {"extends": "basic"}
        return {"extends": "basic",
                "git-gate": {"forges": {self.forge.key: dict(self.forge.description)},
                             "repos": [self.grant()]}}

    def lines(self) -> list[str]:
        if self.origin and self.forge:
            repo = f"Repos   {self.forge.key}/{self.origin.owner}/{self.origin.repo}"
            repos = f"{repo}   {'write' if self.writable else 'read'}"
        elif self.origin:
            repos = f"Repos   none ({self.origin.host} isn't a connected forge)"
        else:
            repos = "Repos   none (no origin remote)"
        return [repos, "Reach   basic: package registries and public code, read-only"]


def parse_origin(url: str) -> Origin | None:
    """`ssh://u@h[:p]/o/r(.git)`, `u@h:o/r(.git)` or `https://h[:p]/o/r(.git)`."""
    if "://" in url:
        parsed = urlparse(url)
        host, port, path = parsed.hostname or "", str(parsed.port or ""), parsed.path
        ssh = parsed.scheme == "ssh"
    else:
        m = _SCP_RE.match(url)
        if not m:
            return None
        host, port, path, ssh = m.group(1), "", m.group(2), True
    parts = [p for p in path.strip("/").split("/") if p]
    if not host or len(parts) != 2:
        return None
    return Origin(host=host, port=port, owner=parts[0], repo=parts[1].removesuffix(".git"),
                  ssh=ssh)


def read_origin(cwd: Path) -> Origin | None:
    result = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=False)
    return parse_origin(result.stdout.strip()) if result.returncode == 0 else None


def find_forge(origin: Origin) -> ForgeMatch | None:
    """The forge this host connected whose web or SSH address is `origin`'s
    host (and port, when both give one)."""
    # pylint: disable=import-outside-toplevel
    from .forge_connections import ForgeConnectionError, connections
    from .manifest.forges import described_connection
    try:
        found = connections()
    except ForgeConnectionError:
        return None
    for key, conn in sorted(found.items()):
        ssh_host, ssh_port = ssh_host_port(conn.ssh) if conn.ssh else ("", "")
        web = urlparse(conn.url)
        # An SSH origin's port is the forge's SSH port: compare it with the
        # SSH address only. Its host may still be the web one (a forge whose
        # SSH address is an IP), so the web host matches it on any port.
        web_port_ok = origin.ssh or origin.port in ("", str(web.port or ""))
        if (origin.host == ssh_host and origin.port in ("", ssh_port)) or (
                origin.host == web.hostname and web_port_ok):
            return ForgeMatch(key=key, description=tuple(described_connection(conn).items()))
    return None


Ask = Callable[[str], str]
Say = Callable[[str], None]


def onboard(cwd: Path, *, ask: Ask, say: Say,
            connect: Callable[[str], bool]) -> str | None:
    """Draft the project's bottle, let the user adjust it, and write it. The
    bottle to launch with (`./main`), or None to skip (use `basic` this once)."""
    origin = read_origin(cwd)
    forge = find_forge(origin) if origin else None
    if origin and forge is None:
        say(f"origin is on {origin.host}, a forge this host hasn't connected.")
        if ask("Connect it now? [y/N] ").strip().lower() in ("y", "yes"):
            default = f"https://{origin.host}"
            url = ask(f"Forge URL [{default}]: ").strip() or default
            if connect(url):
                forge = find_forge(origin)
    draft = Draft(origin=origin, forge=forge)
    while True:
        say(f"No bottle for this project yet. Drafted {bottle_ref(PROJECT_BOTTLE, True)}:")
        for line in draft.lines():
            say(f"  {line}")
        keys = "[enter] use it" + ("   [w] make the repo writable" if forge else "")
        reply = ask(f"{keys}   [s] skip (basic, this once): ").strip().lower()
        if reply == "s":
            return None
        if reply == "w" and forge:
            draft.writable = not draft.writable
            continue
        if reply == "":
            break
    return write_project_bottle(cwd, draft)


def write_project_bottle(cwd: Path, draft: Draft) -> str:
    """Write the draft as `./main` and trust it; its name."""
    # pylint: disable-next=import-outside-toplevel
    from .approvals import project_bottle_path, trust_project_bottle
    path = project_bottle_path(cwd)
    body = ("The bottle for this project, drafted by bot-bottle. It's shared with\n"
            "the repo: review changes to it like any other code.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{serialize_yaml_subset(draft.frontmatter())}---\n\n{body}\n")
    trust_project_bottle(cwd, PROJECT_BOTTLE)
    return bottle_ref(PROJECT_BOTTLE, True)


__all__ = ["Draft", "ForgeMatch", "Origin", "find_forge", "onboard", "parse_origin",
           "read_origin", "write_project_bottle"]
