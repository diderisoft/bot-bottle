"""What a host controller needs to finish a bottle it didn't launch.

A bottle's holder thread tears it down when its agent exits. After a host
controller restart the agent keeps running but the holder is gone, so at launch
the controller records this beside the bottled agent (`bottled_agents`, in the
host DB). The next controller reads it to save the agent's work, tear the
bottle down and revoke its deploy keys. It holds names and ids only, never
secrets. Only the host controller reads or writes it.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import bottle_state
from ..git_gate.provision import DeployKeyRef, deploy_key_refs

if TYPE_CHECKING:
    from .base import Bottle
    from .plan import BottlePlan


@dataclass(frozen=True)
class AdoptedWorkspace:
    """The workspace fields saving the agent's work needs (the same names as
    the plan's workspace)."""

    enabled: bool = False
    has_host_git_dir: bool = False
    guest_path: str = ""


@dataclass(frozen=True)
class AdoptedCredentials:
    """How to keep the bottle's forwarded host login current (none of it
    secret): the provider's refresh spec, and each `token_ref`'s egress slots."""

    harness: str
    token_refs: tuple[str, ...]
    guest_dir: str = ""
    slots: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class AdoptedCredEntry:
    """One `credentials:` block entry persisted for restart re-minting."""

    label: str
    source: str
    kind: str
    settings: dict[str, Any]  # JSON-serializable source-specific settings
    # Maps kind field name ("token") to the EGRESS_TOKEN_N slots that carry it.
    slots: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class AdoptedCredBlock:
    """The `credentials:` block entries that need live minted values, persisted
    so a restarted host controller can re-mint and re-register them."""

    entries: tuple[AdoptedCredEntry, ...]


@dataclass(frozen=True)
class AdoptionRecord:
    container: str
    registry_id: str
    workspace: AdoptedWorkspace = AdoptedWorkspace()
    deploy_keys: tuple[DeployKeyRef, ...] = ()
    credentials: AdoptedCredentials | None = None
    # The bottle's `preserve_container`: its teardown keeps the guest when the
    # bottled agent is kept for resume.
    preserve_container: bool = False
    # `credentials:` block entries that need live minted values on restart.
    cred_block: AdoptedCredBlock | None = None


def record_for(plan: "BottlePlan", bottle: "Bottle") -> AdoptionRecord:
    """The record for a bottle its holder just launched."""
    ws = plan.workspace_plan
    return AdoptionRecord(
        container=bottle.name,
        registry_id=bottle.registry_id,
        workspace=AdoptedWorkspace(
            enabled=ws.enabled, has_host_git_dir=ws.has_host_git_dir,
            guest_path=ws.guest_path),
        deploy_keys=deploy_key_refs(plan.manifest.bottle),
        credentials=credentials_for(plan),
        preserve_container=getattr(plan.manifest.bottle, "preserve_container", False) is True,
        cred_block=cred_block_for(plan),
    )


def cred_block_for(plan: "BottlePlan") -> AdoptedCredBlock | None:
    """The `credentials:` block entries that need live values on restart."""
    # pylint: disable-next=import-outside-toplevel
    from ..egress.service import cred_token_ref
    credentials = getattr(plan.manifest.bottle, "credentials", {})
    if not credentials:
        return None
    entries: list[AdoptedCredEntry] = []
    for label, cred in credentials.items():
        if cred.source == "secret":
            continue
        token_ref = cred_token_ref(label, cred)
        matching = [slot for slot, ref in plan.egress_plan.token_env_map.items()
                    if ref == token_ref]
        if not matching:
            continue
        entries.append(AdoptedCredEntry(
            label=label, source=cred.source, kind=cred.kind,
            settings=dict(cred.settings),
            slots={"token": matching} if cred.kind == "token" else {},
        ))
    return AdoptedCredBlock(tuple(entries)) if entries else None


def credentials_for(plan: "BottlePlan") -> AdoptedCredentials | None:
    """The bottle's forwarded host login, if any, and the egress slots it
    fills."""
    # pylint: disable-next=import-outside-toplevel
    from ..agent_provider import get_provider
    if not getattr(getattr(plan, "agent_provision", None), "provisioned_env", None):
        return None  # nothing forwarded
    spec = get_provider(plan.harness).credential_refresh_spec(plan)
    if spec is None:
        return None
    slots: dict[str, tuple[str, ...]] = {}
    for slot, ref in plan.egress_plan.token_env_map.items():
        if ref in spec.token_refs:
            slots[ref] = (*slots.get(ref, ()), slot)
    return AdoptedCredentials(spec.harness, spec.token_refs, spec.guest_dir, slots)


def write(identity: str, record: AdoptionRecord) -> bool:
    """Keep `record` with `identity`'s; False when nothing records it."""
    return bottle_state.store().set_adoption(identity, json.dumps(dataclasses.asdict(record)))


def read(identity: str) -> AdoptionRecord | None:
    """The record for `identity`, or None if there's none."""
    try:
        raw: Any = json.loads(bottle_state.store().adoption(identity))
        return AdoptionRecord(
            container=str(raw["container"]),
            registry_id=str(raw["registry_id"]),
            workspace=AdoptedWorkspace(**raw["workspace"]),
            deploy_keys=tuple(DeployKeyRef(**k) for k in raw["deploy_keys"]),
            credentials=_credentials(raw["credentials"]),
            preserve_container=raw["preserve_container"] is True,
            cred_block=_cred_block(raw.get("cred_block")),
        )
    except (ValueError, KeyError, TypeError):
        return None


def _credentials(raw: Any) -> AdoptedCredentials | None:
    if not isinstance(raw, dict):
        return None
    return AdoptedCredentials(
        harness=str(raw["harness"]), token_refs=tuple(raw["token_refs"]),
        guest_dir=str(raw["guest_dir"]),
        slots={str(k): tuple(v) for k, v in raw["slots"].items()})


def _cred_block(raw: Any) -> AdoptedCredBlock | None:
    if not isinstance(raw, dict):
        return None
    entries = []
    for e in raw.get("entries", []):
        try:
            entries.append(AdoptedCredEntry(
                label=str(e["label"]), source=str(e["source"]), kind=str(e["kind"]),
                settings=dict(e.get("settings") or {}),
                slots={str(k): list(v) for k, v in (e.get("slots") or {}).items()},
            ))
        except (KeyError, TypeError):
            continue
    return AdoptedCredBlock(tuple(entries)) if entries else None


__all__ = [
    "AdoptedCredBlock", "AdoptedCredEntry", "AdoptedCredentials", "AdoptedWorkspace",
    "AdoptionRecord", "cred_block_for", "credentials_for",
    "read", "record_for", "write",
]
