"""Per-agent git-gate.

The git-gate fronts a bottle's declared git upstreams as a transparent mirror:
a `pre-receive` hook gitleaks-scans pushes and forwards clean refs to the real
upstream with a gate-resident credential; an `--access-hook` refreshes from the
upstream before fetches. The agent never sees the upstream credential.

Layout:

  * `service`  — the `GitGate` host-side service (prepare / provision / revoke /
    preflight) the backend drives at launch.
  * `plan`     — `GitGatePlan`, the launch DTO (in the backend contract).
  * `provision`, `host_key` — the deploy-key + host-key host-side helpers the
    service delegates to.

The rendering, including the hook scripts the gateway runs, lives in
`bot_bottle.control.git_gate_render`; import it from there. This package's
public names are re-exported lazily via `__getattr__`, so importing
`git_gate.plan` (the contract's dependency) doesn't drag in the provisioning /
forge-API code.
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


# Public name -> this package's submodule that defines it.
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
