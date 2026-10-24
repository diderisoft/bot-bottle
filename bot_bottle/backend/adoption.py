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
from dataclasses import dataclass
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
class AdoptionRecord:
    container: str
    registry_id: str
    workspace: AdoptedWorkspace = AdoptedWorkspace()
    deploy_keys: tuple[DeployKeyRef, ...] = ()


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
    )


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
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


__all__ = ["AdoptedWorkspace", "AdoptionRecord", "read", "record_for", "write"]
