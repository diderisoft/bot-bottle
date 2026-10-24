"""Host side of git-gate: `service` (`GitGate`), `plan` (`GitGatePlan`), and
the deploy-key and host-key helpers. Rendering is in
`bot_bottle.control.git_gate_render`. Names are re-exported lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .service import GitGate
    from .plan import GitGatePlan
    from .provision import (
        provision_git_gate_dynamic_keys,
        revoke_git_gate_provisioned_keys,
    )


# Public name -> defining submodule.
_LAZY: dict[str, str] = {
    "GitGate": ".service",
    "GitGatePlan": ".plan",
    "provision_git_gate_dynamic_keys": ".provision",
    "revoke_git_gate_provisioned_keys": ".provision",
    "_provision_dynamic_key": ".provision",
    "_resolve_identity_file": ".provision",
}


def __getattr__(name: str) -> Any:
    src = _LAZY.get(name)
    if src is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(src, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "GitGate",
    "GitGatePlan",
    "provision_git_gate_dynamic_keys",
    "revoke_git_gate_provisioned_keys",
]
