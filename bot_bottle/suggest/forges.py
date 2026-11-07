"""Forges a proposal may grant on that this host hasn't connected yet.

A grant on a forge the host hasn't connected fails to load (ADR 0019), so the
architect used to leave the repo out and say why in a note: a dead end. A
forge is known without a connection when a bottle describes it
(`git-gate.forges.<host>`) or the project's `origin` is on it. A grant on a
known forge is kept, with the forge's description, and the review offers to
connect it before the launch (`c`).
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
from ..manifest.util import ManifestError

Described = dict[str, dict[str, Any]]


def connected_hosts() -> set[str]:
    """The forges this host connected (none when its file doesn't read)."""
    # pylint: disable-next=import-outside-toplevel
    from ..forge_connections import ForgeConnectionError, connections
    try:
        return set(connections())
    except ForgeConnectionError:
        return set()


def _in_bottles(dirs: list[tuple[Path, bool]]) -> Described:
    """Each forge a bottle in `dirs` describes, nearest dir first."""
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.forges import forges_raw
    out: Described = {}
    for directory, _ in dirs:
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            try:
                fm = parse_frontmatter(path.read_text())[0]
                found = forges_raw(path.stem, fm)
            except (OSError, YamlSubsetError, ManifestError):
                continue
            for host, desc in found.items():
                if isinstance(desc, dict):
                    out.setdefault(host, dict(desc))
    return out


def _origin(cwd: Path | None) -> Described:
    """The project's origin forge, when it's one bot-bottle connects (asked
    briefly, as `forge connect` does) and isn't connected here."""
    # pylint: disable=import-outside-toplevel
    from ..forge_kinds import detect
    from ..onboarding import find_forge, read_origin
    origin = read_origin(cwd) if cwd is not None else None
    if origin is None or find_forge(origin) is not None:
        return {}
    try:
        ipaddress.ip_address(origin.host)
        return {}  # an address (a VPN's, say) isn't the forge's name
    except ValueError:
        pass
    kind = detect(f"https://{origin.host}")
    return {origin.host: {"kind": kind}} if kind else {}


def described_forges(dirs: list[tuple[Path, bool]], cwd: Path | None = None) -> Described:
    """Forges this host hasn't connected that a bottle in `dirs` describes,
    or the project at `cwd` is on: host to description."""
    connected = connected_hosts()
    found = {**_origin(cwd), **_in_bottles(dirs)}
    return {host: desc for host, desc in found.items() if host not in connected}


def grant_forge(grant: str) -> str:
    """The forge host a grant is on ("" when it isn't a grant)."""
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.forges import parse_grant, split_grant_alias
    try:
        return parse_grant(split_grant_alias(grant)[1], "a grant")[0]
    except (ManifestError, ValueError):
        return ""


def connect_url(host: str, desc: dict[str, Any]) -> str:
    """The URL `forge connect` takes for `host`."""
    url = desc.get("url")
    return (url if isinstance(url, str) and url else f"https://{host}").rstrip("/")


__all__ = ["Described", "connect_url", "connected_hosts", "described_forges", "grant_forge"]
