"""Granting a repo to a running bottle when the operator approves an agent's
`add-repo` (PRD 0105): find the forge, mint the key and provision the gate
(`git_gate.live_grant`), point the agent's git at it, and optionally keep the
grant in the bottle for later launches."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...deploy_key_provisioner import DeployKeysDisabledError
from ...lib import log

if TYPE_CHECKING:
    from ...manifest.forges import ManifestForge


def find_forge(session: str, forge_name: str) -> "ManifestForge | None":
    """The forge `forge_name` as this host connected it, as the user the
    session's agent pushes as there (its `identity.accounts`, else the forge's
    default); None when the host hasn't connected it."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import session_project
    from ...bottle_state import read_metadata
    from ...forge_connections import get
    from ...manifest.forges import resolve_forge
    from ...manifest.index import ManifestIndex
    from ...manifest.util import ManifestError
    if get(forge_name) is None:
        return None
    user = ""
    metadata = read_metadata(session)
    if metadata is not None and metadata.agent_name:
        project = session_project(session)
        try:
            index = ManifestIndex.resolve(str(project or Path.home()),
                                          missing_ok=True)
            agent = index.load_for_agent(metadata.agent_name, metadata.bottle_names).agent
            user = dict(agent.forge_users).get(forge_name, "")
        except ManifestError:
            pass  # the forge's default
    return resolve_forge(forge_name, None, "", user)


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
    from ...manifest.forges import parse_grant, split_grant_alias
    from ...manifest.util import ManifestError
    from ...secret_source import FileSecrets
    try:
        forge_name = parse_grant(split_grant_alias(grant)[1], "the grant")[0]
        forge = find_forge(identity, forge_name)
        if forge is None:
            raise GrantError(f"this host hasn't connected the forge {forge_name!r}; "
                             f"connect it first")
        entry = grant_entry(grant, forge)
        adopted = read_adoption(identity)
        guest = backend.attach_adopted(identity, adopted) if adopted is not None else None
        if adopted is None or guest is None:
            raise GrantError("can't reach the running bottle")
        provision_live(entry, slug=identity, stage_dir=git_gate_state_dir(identity),
                       secrets=FileSecrets(secrets_file), grant=grant)
        reprovision_git_gate(backend.infra_service().gateway().provisioning_transport(),
                             adopted.registry_id, identity)
        point_agent_at_gate(guest, entry)
    except (GrantError, ManifestError, DeployKeysDisabledError) as e:
        return {"error": str(e)}
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"bottle {identity}: granting {grant} failed: {type(e).__name__}: {e}")
        return {"error": "granting the repo failed; see the host controller's log"}
    log.info(f"bottle {identity}: granted {grant}")
    # pylint: disable-next=import-outside-toplevel
    from ...manifest.forges import described_forge
    return {"repo": entry.Name, "url": entry.Upstream, "writable": not entry.Key.read_only,
            "forge": described_forge(forge)}


def proposed_alias(identity: str, grant: str) -> str:
    """A local name for `grant` when a different repo in the running bottle
    `identity` already has its name: `<repo>-<forge's first label>`
    (`skills-gitea`), numbered if that's taken too. "" when the name is free,
    the grant names its own alias, or the bottle can't be asked."""
    # pylint: disable=import-outside-toplevel
    from ...bottle_state import git_gate_state_dir
    from ...git_gate.live_grant import grant_entry, name_taken
    from ...manifest.forges import parse_grant, split_grant_alias
    try:
        alias, bare = split_grant_alias(grant)
        forge_name, _, repo, _ = parse_grant(bare, "the grant")
        forge = find_forge(identity, forge_name)
        if alias or forge is None:
            return ""
        names = name_taken(grant_entry(bare, forge), git_gate_state_dir(identity))
    except Exception as e:  # pylint: disable=broad-exception-caught  # granting says why
        log.debug(f"bottle {identity}: no alias for {grant}: {type(e).__name__}: {e}")
        return ""
    if names is None:
        return ""
    base = f"{repo}-{forge_name.split('.')[0]}"
    return next(n for n in (base, *(f"{base}-{i}" for i in range(2, 100)), f"{base}-x")
                if n not in names)


__all__ = ["find_forge", "grant_repo", "proposed_alias"]
