"""Per-agent git-gate (PRD 0008).

A third per-agent daemon that fronts the bottle's declared git
upstreams as a transparent mirror. Each `bottle.git` entry maps to
a bare repo on the gate; `git daemon` serves the bare repos over
`git://<gate>/<name>.git`. Two hooks make the mirror bidirectional:

- **`pre-receive`** (push path) — gitleaks-scans incoming refs and,
  on clean, forwards them to the real upstream with the
  gate-resident credential.
- **`--access-hook`** (fetch path) — runs `git fetch origin --prune`
  against the real upstream before every `upload-pack`, so an
  agent fetch returns whatever the upstream has *now*. Fail-closed
  if the upstream is unreachable.

The agent never sees the upstream credential under either path.

Why a separate daemon (not folded into egress or ssh-gate): the
gate is the only one of the three that holds upstream push
credentials. Mixing it with egress would put push creds in the
same blast radius as internet-facing TLS interception; mixing it
with ssh-gate would force ssh-gate above L4 and into git-protocol
land. See `docs/prds/0008-git-gate.md`.

This module defines the abstract gate (`GitGate`) and its plan
dataclass (`GitGatePlan`). The gateway's start/stop lifecycle is
backend-specific and lives on concrete subclasses (see
`bot_bottle/backend/docker/git_gate.py`)."""


from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from pathlib import Path

from .manifest import ManifestBottle

# Rendering and the deploy-key lifecycle live in sibling modules; the
# names are re-exported here (see __all__) so existing
# `from bot_bottle.git_gate import …` callers are unchanged.
from .git_gate_render import (
    GIT_GATE_HOSTNAME,
    GIT_GATE_TIMEOUT_SECS,
    GitGateUpstream,
    git_gate_known_hosts_line,
    git_gate_render_access_hook,
    git_gate_render_entrypoint,
    git_gate_render_provision,
    git_gate_render_gitconfig,
    git_gate_render_hook,
    git_gate_upstreams_for_bottle,
    _gitconfig_validate_value,
)
from .git_gate_provision import (
    provision_git_gate_dynamic_keys,
    revoke_git_gate_provisioned_keys,
    _provision_dynamic_key,
    _resolve_identity_file,
)

@dataclass(frozen=True)
class GitGatePlan:
    """Output of GitGate.prepare; consumed by .start.

    The script + slug + upstream fields are filled at prepare time
    (host-side, side-effect-free on docker). The network fields are
    populated by the backend's launch step via `dataclasses.replace`
    once those networks exist. Empty defaults are sentinels meaning
    "not yet set"; `.start` validates that they are populated.

    `hook_script` is the shared `pre-receive` for push-time gating;
    `access_hook_script` is `git daemon`'s `--access-hook` for the
    fetch-time upstream refresh."""

    slug: str
    entrypoint_script: Path
    hook_script: Path
    access_hook_script: Path
    upstreams: tuple[GitGateUpstream, ...]
    internal_network: str = ""
    egress_network: str = ""




class GitGate(ABC):
    """The per-agent git-gate. Encapsulates the host-side prepare
    (upstream lift + entrypoint/hook render); the gateway's
    start/stop lifecycle is backend-specific and lives on concrete
    subclasses."""

    def prepare(self, bottle: ManifestBottle, slug: str, stage_dir: Path) -> GitGatePlan:
        """Compute the upstream table from `bottle.git` and write the
        entrypoint, pre-receive hook, and access-hook scripts (mode
        600) under `stage_dir`. Pure host-side, no docker subprocess.

        For `gitea` key entries, the returned upstream intentionally
        has an empty identity file. Backend launch fills that in after
        the operator confirms the preflight.

        Returned plan is incomplete: the launch step must fill
        `internal_network` / `egress_network` via `dataclasses.replace`
        before passing the plan to `.start`."""
        upstreams = git_gate_upstreams_for_bottle(bottle)
        entrypoint = stage_dir / "git_gate_entrypoint.sh"
        entrypoint.write_text(git_gate_render_entrypoint(upstreams))
        entrypoint.chmod(0o600)
        hook = stage_dir / "git_gate_pre_receive.sh"
        hook.write_text(git_gate_render_hook())
        hook.chmod(0o600)
        access_hook = stage_dir / "git_gate_access_hook.sh"
        access_hook.write_text(git_gate_render_access_hook())
        # 0o700 (not 0o600): git daemon execs --access-hook directly,
        # not via `sh`, so the script needs the x bit. The gateway copy
        # does not necessarily preserve this mode (`docker cp` does, the
        # Apple `container cp` does not), so provision_git_gate re-applies
        # +x on the gateway side — see backend/docker/gateway_provision.py.
        access_hook.chmod(0o700)
        upstreams_with_files: list[GitGateUpstream] = []
        for u in upstreams:
            known_hosts_file = Path()
            if u.known_host_key:
                known_hosts_file = stage_dir / f"{u.name}-known_hosts"
                known_hosts_file.write_text(
                    git_gate_known_hosts_line(
                        u.upstream_host, u.upstream_port, u.known_host_key,
                    )
                )
                known_hosts_file.chmod(0o600)
            upstreams_with_files.append(
                GitGateUpstream(
                    name=u.name,
                    upstream_url=u.upstream_url,
                    upstream_host=u.upstream_host,
                    upstream_port=u.upstream_port,
                    identity_file=u.identity_file,
                    known_host_key=u.known_host_key,
                    known_hosts_file=known_hosts_file,
                )
            )
        return GitGatePlan(
            slug=slug,
            entrypoint_script=entrypoint,
            hook_script=hook,
            access_hook_script=access_hook,
            upstreams=tuple(upstreams_with_files),
        )


__all__ = [
    "GIT_GATE_HOSTNAME",
    "GIT_GATE_TIMEOUT_SECS",
    "GitGateUpstream",
    "GitGatePlan",
    "GitGate",
    "git_gate_upstreams_for_bottle",
    "git_gate_render_gitconfig",
    "git_gate_known_hosts_line",
    "git_gate_render_entrypoint",
    "git_gate_render_provision",
    "git_gate_render_hook",
    "git_gate_render_access_hook",
    "provision_git_gate_dynamic_keys",
    "revoke_git_gate_provisioned_keys",
    "_gitconfig_validate_value",
    "_provision_dynamic_key",
    "_resolve_identity_file",
]
