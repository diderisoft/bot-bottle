"""What a host controller needs to finish a bottle it didn't launch.

A bottle's holder thread tears it down when its agent exits. After a host
controller restart the agent keeps running but the holder is gone, so at launch
the controller writes this record to the bottle's state dir. The next
controller reads it to save the agent's work, tear the bottle down and revoke
its deploy keys. It holds names and ids only, never secrets.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..bottle_state import bottle_state_dir
from ..git_gate.provision import DeployKeyRef, deploy_key_refs

if TYPE_CHECKING:
    from pathlib import Path

    from .base import Bottle
    from .plan import BottlePlan

_FILE = "adoption.json"


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
class AdoptionRecord:
    container: str
    registry_id: str
    workspace: AdoptedWorkspace = AdoptedWorkspace()
    deploy_keys: tuple[DeployKeyRef, ...] = ()
    credentials: AdoptedCredentials | None = None
    # The bottle's `preserve_container`: its teardown keeps the guest when the
    # bottled agent is kept for resume.
    preserve_container: bool = False


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
    )


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


def _path(identity: str) -> "Path":
    return bottle_state_dir(identity) / _FILE


def write(identity: str, record: AdoptionRecord) -> None:
    path = _path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(record), indent=2) + "\n")
    path.chmod(0o600)


def read(identity: str) -> AdoptionRecord | None:
    """The record for `identity`, or None if it's missing or unreadable."""
    try:
        raw: Any = json.loads(_path(identity).read_text())
        return AdoptionRecord(
            container=str(raw["container"]),
            registry_id=str(raw["registry_id"]),
            workspace=AdoptedWorkspace(**raw.get("workspace", {})),
            deploy_keys=tuple(DeployKeyRef(**k) for k in raw.get("deploy_keys", [])),
            credentials=_credentials(raw.get("credentials")),
            preserve_container=raw.get("preserve_container") is True,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _credentials(raw: Any) -> AdoptedCredentials | None:
    if not isinstance(raw, dict):
        return None
    return AdoptedCredentials(
        harness=str(raw["harness"]), token_refs=tuple(raw["token_refs"]),
        guest_dir=str(raw.get("guest_dir", "")),
        slots={str(k): tuple(v) for k, v in raw.get("slots", {}).items()})


__all__ = ["AdoptedCredentials", "AdoptedWorkspace", "AdoptionRecord", "credentials_for",
           "read", "record_for", "write"]
