"""Per-agent git-gate (PRD 0008).

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

The rendering + the in-gateway hook execution live in
`bot_bottle.gateway.git_gate.render`. The public names are re-exported lazily
via `__getattr__`, so `from bot_bottle.git_gate import …` keeps working and
importing `git_gate.plan` (the contract's dependency) doesn't drag in the
provisioning / forge-API code.
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
    from ..gateway.git_gate.render import (
        GIT_GATE_HOSTNAME,
        GIT_GATE_TIMEOUT_SECS,
        GitGateUpstream,
        git_gate_known_hosts_line,
        git_gate_render_access_hook,
        git_gate_render_entrypoint,
        git_gate_render_gitconfig,
        git_gate_render_hook,
        git_gate_render_provision,
        git_gate_upstreams_for_bottle,
    )


# Public name -> relative module that defines it. Render/hook names come from
# the gateway (where the in-gateway execution lives); the service, plan, and
# provisioning are this package's own submodules.
_LAZY: dict[str, str] = {
    "GitGate": ".service",
    "GitGatePlan": ".plan",
    "provision_git_gate_dynamic_keys": ".provision",
    "revoke_git_gate_provisioned_keys": ".provision",
    "_provision_dynamic_key": ".provision",
    "_resolve_identity_file": ".provision",
    "GIT_GATE_HOSTNAME": "..gateway.git_gate.render",
    "GIT_GATE_TIMEOUT_SECS": "..gateway.git_gate.render",
    "GitGateUpstream": "..gateway.git_gate.render",
    "git_gate_upstreams_for_bottle": "..gateway.git_gate.render",
    "git_gate_render_gitconfig": "..gateway.git_gate.render",
    "git_gate_known_hosts_line": "..gateway.git_gate.render",
    "git_gate_render_entrypoint": "..gateway.git_gate.render",
    "git_gate_render_provision": "..gateway.git_gate.render",
    "git_gate_render_hook": "..gateway.git_gate.render",
    "git_gate_render_access_hook": "..gateway.git_gate.render",
    "_gitconfig_validate_value": "..gateway.git_gate.render",
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
    "GitGateUpstream",
    "GIT_GATE_HOSTNAME",
    "GIT_GATE_TIMEOUT_SECS",
    "git_gate_upstreams_for_bottle",
    "git_gate_render_gitconfig",
    "git_gate_known_hosts_line",
    "git_gate_render_entrypoint",
    "git_gate_render_provision",
    "git_gate_render_hook",
    "git_gate_render_access_hook",
    "provision_git_gate_dynamic_keys",
    "revoke_git_gate_provisioned_keys",
]
