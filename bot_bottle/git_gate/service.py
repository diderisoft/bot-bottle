"""The `GitGate` host-side service (PRD 0008).

The git-gate fronts a bottle's declared git upstreams as a transparent mirror:
a `pre-receive` hook gitleaks-scans pushes and forwards clean refs to the real
upstream with a gate-resident credential; an `--access-hook` refreshes from the
upstream before fetches. The agent never sees the upstream credential.

`GitGate` is the host-side service the backend drives at launch: build the plan
(`prepare`), provision / revoke the per-upstream deploy keys, and preflight the
upstream host keys. The rendering it emits + the in-gateway hook execution live
in `bot_bottle.gateway.git_gate.render`.
"""

from __future__ import annotations

from pathlib import Path

from ..manifest import Manifest, ManifestBottle
from ..gateway.git_gate.render import (
    GitGateUpstream,
    git_gate_known_hosts_line,
    git_gate_render_access_hook,
    git_gate_render_entrypoint,
    git_gate_render_gitleaks_config,
    git_gate_render_hook,
    git_gate_upstreams_for_bottle,
)
from .plan import GitGatePlan
from . import provision as _provision
from . import host_key as _host_key



def stage_git_gate_scripts(
    stage_dir: Path, upstreams: tuple[GitGateUpstream, ...],
) -> tuple[Path, Path, Path, Path]:
    """Render the git-gate entrypoint, pre-receive hook, access hook and
    gitleaks config from the current code into `stage_dir`, returning their
    paths in that order. Used at launch and when a restarted gateway is
    re-provisioned (PRD 0081), so both always install current hooks."""
    entrypoint = stage_dir / "git_gate_entrypoint.sh"
    entrypoint.write_text(git_gate_render_entrypoint(upstreams))
    entrypoint.chmod(0o600)
    hook = stage_dir / "git_gate_pre_receive.sh"
    hook.write_text(git_gate_render_hook())
    hook.chmod(0o600)
    access_hook = stage_dir / "git_gate_access_hook.sh"
    access_hook.write_text(git_gate_render_access_hook())
    # 0o700 (not 0o600): git daemon execs --access-hook directly, not via
    # `sh`, so the script needs the x bit. The gateway copy does not
    # necessarily preserve this mode (`docker cp` does, the Apple `container
    # cp` does not), so provisioning re-applies +x on the gateway side — see
    # backend/provision_gateway.py.
    access_hook.chmod(0o700)
    gitleaks_config = stage_dir / "git_gate_gitleaks.toml"
    gitleaks_config.write_text(git_gate_render_gitleaks_config())
    gitleaks_config.chmod(0o600)
    return entrypoint, hook, access_hook, gitleaks_config

class GitGate:
    """The per-agent git-gate host-side service. Stateless — the backend's
    launch step drives `prepare` → `provision_dynamic_keys` and, at teardown,
    `revoke_provisioned_keys`; `preflight_host_keys` gates a launch on the
    upstream host keys being known."""

    def prepare(self, bottle: ManifestBottle, slug: str, stage_dir: Path) -> GitGatePlan:
        """Compute the upstream table from `bottle.git` and write the
        entrypoint, pre-receive hook, and access-hook scripts (mode 600) under
        `stage_dir`. Pure host-side, no docker subprocess.

        For `gitea` key entries, the returned upstream intentionally has an
        empty identity file. Backend launch fills that in after the operator
        confirms the preflight.

        Returned plan is incomplete: the launch step must fill
        `internal_network` / `egress_network` via `dataclasses.replace` before
        passing the plan to `.start`."""
        upstreams = git_gate_upstreams_for_bottle(bottle)
        entrypoint, hook, access_hook, gitleaks_config = stage_git_gate_scripts(
            stage_dir, upstreams)
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
            gitleaks_config=gitleaks_config,
            upstreams=tuple(upstreams_with_files),
        )

    def provision_dynamic_keys(
        self, bottle: ManifestBottle, plan: GitGatePlan, stage_dir: Path,
    ) -> GitGatePlan:
        """Mint the `gitea` upstreams' ephemeral deploy keys via the forge API
        and return the plan with their identity files filled in."""
        return _provision.provision_git_gate_dynamic_keys(bottle, plan, stage_dir)

    def revoke_provisioned_keys(self, bottle: ManifestBottle, stage_dir: Path) -> None:
        """Revoke every deploy key provisioned for `bottle` (teardown)."""
        _provision.revoke_git_gate_provisioned_keys(bottle, stage_dir)

    def preflight_host_keys(
        self, manifest: Manifest, *, headless: bool, home_md: Path | None,
    ) -> Manifest:
        """Ensure every git-gate upstream has a known host key, populating
        missing ones (or erroring in headless mode). Returns the manifest,
        possibly updated with fetched keys."""
        return _host_key.preflight_host_keys(
            manifest, headless=headless, home_md=home_md,
        )
