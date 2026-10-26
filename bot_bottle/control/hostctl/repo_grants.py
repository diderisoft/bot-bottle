"""Granting a repo to a running bottle when the operator approves an agent's
`add-repo` (PRD 0105): find the forge, mint the key and provision the gate
(`git_gate.live_grant`), point the agent's git at it, and optionally keep the
grant in the bottle for later launches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib import log

if TYPE_CHECKING:
    from ...manifest.forges import ManifestForge


@dataclass(frozen=True)
class FoundForge:
    """A forge a grant names. `bottle` is the host bottle declaring it when no
    bottle the session launched with does (keeping the grant then extends it)."""

    forge: "ManifestForge"
    bottle: str = ""


def _forges(name: str, dirs: list[tuple[Path, bool]]) -> tuple["ManifestForge", ...]:
    # pylint: disable=import-outside-toplevel
    from ...manifest.loader import load_bottle_chain
    from ...manifest.util import ManifestError
    try:
        return load_bottle_chain(name, dirs)[0].forges
    except ManifestError:
        return ()


def find_forge(session: str, forge_name: str) -> FoundForge | None:
    """The forge `forge_name` from the bottles `session` launched with, as their
    files are now (so one connected since launch counts), else from any host
    bottle."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import session_project
    from ...bottle_state import read_metadata
    from ...manifest.index import ManifestIndex
    from ...paths import bot_bottle_root
    host_bottles = bot_bottle_root() / "bottles"
    project = session_project(session)
    dirs = (ManifestIndex.resolve(str(project)).bottle_dirs() if project is not None
            else [(host_bottles, False)])
    metadata = read_metadata(session)
    for name in metadata.bottle_names if metadata else ():
        for forge in _forges(name, dirs):
            if forge.name == forge_name:
                return FoundForge(forge)
    for path in sorted(host_bottles.glob("*.md")):
        for forge in _forges(path.stem, [(host_bottles, False)]):
            if forge.name == forge_name:
                return FoundForge(forge, path.stem)
    return None


def grant_repo(backend: Any, identity: str, grant: str, *,
               secrets_file: Path) -> dict[str, object]:
    """Grant `grant` (`<forge>/<owner>/<repo>[:write]`) to the running bottle
    `identity`. The reply's `repo`, `url`, `writable`, or `error`."""
    # pylint: disable=import-outside-toplevel
    from ...backend.adoption import read as read_adoption
    from ...backend.gateway_attach import reprovision_git_gate
    from ...bottle_state import git_gate_state_dir
    from ...git_gate.live_grant import (
        GrantError, grant_entry, point_agent_at_gate, provision_live,
    )
    from ...manifest.forges import parse_grant
    from ...manifest.util import ManifestError
    from ...secret_source import FileSecrets
    try:
        forge_name = parse_grant(grant, "the grant")[0]
        found = find_forge(identity, forge_name)
        if found is None:
            raise GrantError(f"no bottle on this host declares the forge {forge_name!r}; "
                             f"connect it first")
        entry = grant_entry(grant, found.forge)
        adopted = read_adoption(identity)
        guest = backend.attach_adopted(identity, adopted) if adopted is not None else None
        if adopted is None or guest is None:
            raise GrantError("can't reach the running bottle")
        provision_live(entry, slug=identity, stage_dir=git_gate_state_dir(identity),
                       secrets=FileSecrets(secrets_file), grant=grant)
        reprovision_git_gate(backend.infra_service().gateway().provisioning_transport(),
                             adopted.registry_id, identity)
        point_agent_at_gate(guest, entry)
    except (GrantError, ManifestError) as e:
        return {"error": str(e)}
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"bottle {identity}: granting {grant} failed: {type(e).__name__}: {e}")
        return {"error": "granting the repo failed; see the host controller's log"}
    log.info(f"bottle {identity}: granted {grant}")
    return {"repo": entry.Name, "url": entry.Upstream, "writable": not entry.Key.read_only,
            "extends": found.bottle}


__all__ = ["FoundForge", "find_forge", "grant_repo"]
